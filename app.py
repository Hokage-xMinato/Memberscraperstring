import os
from flask import Flask, request, jsonify, send_from_directory
from telethon import TelegramClient # Removed .sync here
from telethon.sessions import StringSession 
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty, InputPeerChannel, InputPeerUser
from telethon.errors.rpcerrorlist import PeerFloodError, UserPrivacyRestrictedError, SessionPasswordNeededError, PhoneNumberInvalidError
from telethon.tl.functions import users 
from telethon.tl.functions.channels import InviteToChannelRequest 
import csv
import io
import traceback
import time
import re
import threading
import json
import asyncio # Import asyncio for event loop management

app = Flask(__name__, static_folder='static', static_url_path='') 

# Global client state, managed by the main Flask thread
_telegram_client = None
_client_lock = threading.Lock() 

API_ID = os.environ.get('TELETHON_API_ID')
API_HASH = os.environ.get('TELETHON_API_HASH')
PHONE_NUMBER = os.environ.get('TELETHON_PHONE_NUMBER') 
STRING_SESSION_ENV = os.environ.get('TELETHON_STRING_SESSION') 

# Define a function to get the current event loop, or create a new one if none exists.
# This helps in environments where an event loop might not be readily available for non-main threads.
def get_or_create_event_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

# Modified get_telegram_client to use async operations for initial connection checks
async def get_telegram_client_async():
    """Initializes or returns the global TelegramClient for the main Flask thread."""
    global _telegram_client
    # Acquire lock for the global client state
    # This lock is held across the async operation to prevent race conditions during client creation/connection
    with _client_lock: 
        if _telegram_client is None:
            if not API_ID or not API_HASH:
                raise ValueError("TELETHON_API_ID and TELETHON_API_HASH environment variables must be set.")
            if not PHONE_NUMBER: 
                 raise ValueError("TELETHON_PHONE_NUMBER environment variable must be set (required for re-authentication).")
            
            session_obj = StringSession(STRING_SESSION_ENV) if STRING_SESSION_ENV else StringSession(None)
            _telegram_client = TelegramClient(session_obj, int(API_ID), API_HASH)
            
            # Connect the client when it's first created
            # This is crucial for Telethon 1.x which expects connect before many operations
            if not _telegram_client.is_connected():
                print("Connecting main Telegram client...")
                await _telegram_client.connect()
                print("Main Telegram client connected.")
        return _telegram_client

# Decorator to handle running async Flask routes
def run_async(f):
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper

@app.route('/')
def serve_index():
    """Serves the index.html file from the static folder."""
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/get_initial_status', methods=['GET', 'POST']) 
@run_async # Decorator to run this async function synchronously for Flask
async def get_initial_status():
    """
    Returns the initial status of the Telegram client (connected, needs auth code, etc.)
    and pre-fills UI fields with environment variable values.
    """
    current_phone_number_display = PHONE_NUMBER + " (from env var)" if PHONE_NUMBER else "Not set" 
    
    try:
        client = await get_telegram_client_async() # Await the async client getter
        
        connected = await client.is_user_authorized() # Await async method
        needs_auth_code = False
        
        if connected:
            me_obj = await client.get_me() # Await async method
            if me_obj and me_obj.phone:
                current_phone_number_display = f"+{me_obj.phone}" 
            else:
                current_phone_number_display = PHONE_NUMBER + " (from env var, not found in profile)" 
        elif not STRING_SESSION_ENV: 
            needs_auth_code = True 
        
        return jsonify({
            "api_id": API_ID,
            "api_hash": API_HASH,
            "phone_number": current_phone_number_display, 
            "connected": connected,
            "needs_auth_code": needs_auth_code
        })
    except ValueError as e: 
        return jsonify({"error": str(e), "connected": False, "api_id": API_ID, "api_hash": API_HASH, "phone_number": current_phone_number_display}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Internal server error: {e}", "connected": False, "api_id": API_ID, "api_hash": API_HASH, "phone_number": current_phone_number_display}), 500


@app.route('/send_code', methods=['POST'])
@run_async # Decorator to run this async function synchronously for Flask
async def send_code_route():
    """
    Handles sending the verification code to the user's phone.
    Uses environment variables for API credentials and phone.
    """
    if not API_ID or not API_HASH or not PHONE_NUMBER:
        return jsonify({"error": "TELETHON_API_ID, TELETHON_API_HASH, and TELETHON_PHONE_NUMBER environment variables must be set."}), 400

    try:
        client = await get_telegram_client_async() # Await the async client getter
        
        if not await client.is_user_authorized(): # Await async method
            await client.send_code_request(PHONE_NUMBER) # Await async method
            return jsonify({"message": "Verification code sent. Please enter it."})
        else:
            return jsonify({"message": "Already authorized."})
    except PhoneNumberInvalidError:
        return jsonify({"error": "The phone number from TELETHON_PHONE_NUMBER env var is invalid. Please check it."}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/sign_in', methods=['POST'])
@run_async # Decorator to run this async function synchronously for Flask
async def sign_in_route():
    """
    Handles signing in with the verification code.
    Requires phone code from frontend. Uses env vars for phone, API details.
    """
    data = request.json
    phone_code = data.get('phone_code')

    if not phone_code:
        return jsonify({"error": "Missing phone code."}), 400
    if not API_ID or not API_HASH or not PHONE_NUMBER:
        return jsonify({"error": "TELETHON_API_ID, TELETHON_API_HASH, and TELETHON_PHONE_NUMBER environment variables must be set."}), 400

    try:
        client = await get_telegram_client_async() # Await the async client getter
        
        await client.sign_in(PHONE_NUMBER, phone_code) # Await async method
        string_session = client.session.save()
        
        global STRING_SESSION_ENV
        STRING_SESSION_ENV = string_session 

        print("\n--- NEWLY GENERATED TELETHON STRING SESSION (COPY THIS!) ---")
        print(string_session)
        print("-----------------------------------------------------------\n")
        print("IMPORTANT: Update the 'TELETHON_STRING_SESSION' environment variable on Render with the string above for future persistent logins.")
        
        me_obj = await client.get_me() # Await async method
        current_phone_number_display = f"+{me_obj.phone}" if me_obj and me_obj.phone else PHONE_NUMBER + " (from env var, not found in profile)"

        return jsonify({"message": "Signed in successfully! Session printed to logs. Please update Render ENV var.", "phone_number": current_phone_number_display})
    except SessionPasswordNeededError:
        return jsonify({"error": "Two-factor authentication is enabled. This app does not support it currently. Please disable 2FA or use a session string from a 2FA-handled login."}), 403
    except Exception as e:
        traceback.print_exc()
        global _telegram_client
        if _telegram_client:
            await _telegram_client.disconnect() # Await async method
            _telegram_client = None # Reset client on failure to force re-initialization
        return jsonify({"error": str(e)}), 500


@app.route('/get_groups', methods=['POST'])
@run_async # Decorator to run this async function synchronously for Flask
async def get_groups_route():
    """Lists available megagroups for the authenticated user."""
    try:
        client = await get_telegram_client_async() # Await the async client getter
        
        if not await client.is_user_authorized(): # Await async method
            return jsonify({"error": "Telegram client not connected or authorized. Please connect/authenticate."}), 401

        result = await client(GetDialogsRequest( # Await async method
            offset_date=None,
            offset_id=0,
            offset_peer=InputPeerEmpty(),
            limit=200,
            hash=0
        ))
        # Filter to include only megagroups (supergroups)
        groups = [
            {'id': chat.id, 'title': chat.title, 'access_hash': chat.access_hash}
            for chat in result.chats if getattr(chat, 'megagroup', False)
        ]
        
        return jsonify({"groups": groups})
    except ValueError as e: 
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/list_members', methods=['POST'])
@run_async # Decorator to run this async function synchronously for Flask
async def list_members_route():
    """Lists members of a selected group and returns them."""
    data = request.json
    group_id_str = data.get('group_id')

    if not group_id_str: 
        return jsonify({"error": "Missing group ID"}), 400

    try:
        client = await get_telegram_client_async() # Await the async client getter
        
        if not await client.is_user_authorized(): # Await async method
            return jsonify({"error": "Telegram client not connected or authorized. Please connect/authenticate."}), 401

        try:
            group_id_int = int(group_id_str)
            # Use client.get_input_entity() which is the most robust way to get an InputPeer from an ID
            target_group_entity = await client.get_input_entity(group_id_int) # Await async method
            
        except ValueError:
            return jsonify({"error": f"Invalid group ID format. ID: '{group_id_str}'"}), 400
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": f"Failed to resolve group entity (ID: '{group_id_str}'). Ensure it's a valid group you have access to. Detail: {str(e)}"}), 500

        participants = await client.get_participants(target_group_entity, aggressive=True) # Await async method
        
        members_data = []
        for user in participants:
            name = ' '.join(filter(None, [user.first_name, user.last_name]))
            members_data.append({
                'username': user.username,
                'user_id': user.id,
                'access_hash': user.access_hash,
                'name': name
            })
        return jsonify({"members": members_data})
    except Exception as e: 
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/add_members', methods=['POST'])
def add_members_route():
    """Initiates adding users from provided CSV data to a selected group in a separate thread."""
    data = request.json
    group_id_str = data.get('group_id')
    group_hash_str = data.get('group_hash') 
    add_method = data.get('add_method')
    csv_data = data.get('csv_data')

    if not all([group_id_str, group_hash_str, add_method, csv_data]):
        return jsonify({"error": "Missing group ID, hash, add method, or CSV data"}), 400

    try:
        # We perform a quick authorization check on the main thread's client first.
        # The actual adding thread will get its own client.
        main_client = asyncio.run(get_telegram_client_async()) # Ensure main client is ready
        if not asyncio.run(main_client.is_user_authorized()):
             return jsonify({"error": "Telegram client not connected or authorized on main thread. Please connect/authenticate."}), 401

        users_to_add = []
        csv_file = io.StringIO(csv_data)
        reader = csv.reader(csv_file, delimiter=',', lineterminator='\n')
        next(reader)  # Skip header
        for row in reader:
            if len(row) < 3:
                print(f'Skipping incomplete user record: {row}')
                continue
            users_to_add.append({
                'username': row[0],
                'id': int(row[1]) if row[1] else 0,
                'access_hash': int(row[2]) if row[2] else 0
            })

        # --- CRITICAL CHANGE FOR THREADED OPERATION ---
        # Pass all necessary credentials and data to the thread function.
        # The thread will manage its own asyncio loop and client instance.
        add_thread = threading.Thread(target=lambda: asyncio.run(_add_members_threaded_async(
            API_ID, 
            API_HASH, 
            STRING_SESSION_ENV, 
            int(group_id_str), # Pass as int directly
            int(group_hash_str), # Pass as int directly
            users_to_add, 
            int(add_method)
        )))
        add_thread.start()

        return jsonify({"message": f"Adding members initiated for {len(users_to_add)} users. Progress will be logged on the server. Please allow time for completion due to Telegram's rate limits (60s per user)."}), 202

    except Exception as e: 
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# --- Asynchronous function to run in the separate thread ---
async def _add_members_threaded_async(api_id, api_hash, string_session_env, group_id_int, group_hash_int, users_list, add_method):
    """
    This asynchronous function runs in a separate thread,
    creates its own TelegramClient instance, and manages its own event loop.
    """
    thread_client = None
    try:
        print(f"THREAD DEBUG: Initializing async client in new thread. API_ID: {api_id}")
        session_obj = StringSession(string_session_env) if string_session_env else StringSession(None)
        thread_client = TelegramClient(session_obj, int(api_id), api_hash)
        
        # Connect and ensure authorization within this thread's client
        await thread_client.connect()
        if not await thread_client.is_user_authorized():
            print("THREAD ERROR: Client in new thread is not authorized. Stopping add operation.")
            return # Exit thread

        print(f"THREAD DEBUG: Async client connected and authorized in thread for group ID: {group_id_int}")
        
        # Resolve the target group entity within this thread's client context
        target_group_entity = await thread_client.get_input_entity(group_id_int)
        
        added_count = 0
        skipped_count = 0
        errors = []

        for user in users_list:
            try:
                print(f'Attempting to add {user["username"] or user["id"]}')
                user_entity = None
                if add_method == 1:  # by username
                    if not user['username']:
                        errors.append(f'Skipping user {user["id"]} due to missing username for method 1.')
                        skipped_count += 1
                        continue
                    user_entity = await thread_client.get_input_entity(user['username']) # Await here
                elif add_method == 2:  # by ID
                    if not user['id'] or not user['access_hash']:
                        errors.append(f'Skipping user {user["username"]} due to missing ID/Access Hash for method 2.')
                        skipped_count += 1
                        continue
                    user_entity = InputPeerUser(user['id'], user['access_hash'])
                else:
                    errors.append(f'Invalid add method specified for user {user["username"] or user["id"]}.')
                    break

                if user_entity:
                    await thread_client(InviteToChannelRequest(target_group_entity, [user_entity])) # Await here
                    added_count += 1
                    print(f'Successfully added {user["username"] or user["id"]}. Waiting 60 seconds...')
                    await asyncio.sleep(60) # Use asyncio.sleep for async functions
            except PeerFloodError:
                errors.append('PeerFloodError: Too many requests. Stopping addition.')
                print('Flood error. Stopping.')
                break
            except UserPrivacyRestrictedError:
                skipped_count += 1
                errors.append(f'UserPrivacyRestrictedError for {user["username"] or user["id"]}: Privacy restrictions. Skipping.')
                print(f'Privacy restrictions for {user["username"] or user["id"]}. Skipping.')
            except Exception as e:
                skipped_count += 1
                errors.append(f'Error adding {user["username"] or user["id"]}: {str(e)}')
                traceback.print_exc()

        final_message = (f'Finished adding members. '
                         f'Added: {added_count}, Skipped: {skipped_count}. '
                         f'Total users processed: {len(users_list)}. '
                         f'Errors: {"; ".join(errors) if errors else "None."}')
        print(final_message)

    except Exception as e:
        print(f"THREAD CRITICAL ERROR: An unexpected error occurred in the adding thread: {e}")
        traceback.print_exc()
    finally:
        if thread_client and thread_client.is_connected():
            print("THREAD DEBUG: Disconnecting client in thread.")
            await thread_client.disconnect() # Await here


if __name__ == '__main__':
    # Flask is still synchronous, so wrap its run call in asyncio.run if you're mixing async in main thread,
    # but for a typical Gunicorn deployment, you wouldn't directly run this with asyncio.run().
    # Gunicorn handles the event loop for Flask.
    # The key is ensuring each Telethon interaction (even in main Flask routes) happens within an event loop.
    # We use @run_async decorator for Flask routes to manage this.
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
