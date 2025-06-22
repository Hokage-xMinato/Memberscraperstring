import os
from flask import Flask, request, jsonify, send_from_directory
from telethon.sync import TelegramClient
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

app = Flask(__name__, static_folder='static', static_url_path='') 

# Global client state, managed by the main Flask thread
_telegram_client = None
_client_lock = threading.Lock() 

API_ID = os.environ.get('TELETHON_API_ID')
API_HASH = os.environ.get('TELETHON_API_HASH')
PHONE_NUMBER = os.environ.get('TELETHON_PHONE_NUMBER') 
STRING_SESSION_ENV = os.environ.get('TELETHON_STRING_SESSION') 

def get_telegram_client():
    """Initializes or returns the global TelegramClient for the main Flask thread."""
    global _telegram_client
    with _client_lock:
        if _telegram_client is None:
            if not API_ID or not API_HASH:
                raise ValueError("TELETHON_API_ID and TELETHON_API_HASH environment variables must be set.")
            if not PHONE_NUMBER: 
                 raise ValueError("TELETHON_PHONE_NUMBER environment variable must be set (required for re-authentication).")
            
            # Use StringSession object directly for in-memory session handling
            session_obj = StringSession(STRING_SESSION_ENV) if STRING_SESSION_ENV else StringSession(None)
            _telegram_client = TelegramClient(session_obj, int(API_ID), API_HASH)
        return _telegram_client

@app.route('/')
def serve_index():
    """Serves the index.html file from the static folder."""
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/get_initial_status', methods=['GET', 'POST']) 
def get_initial_status():
    """
    Returns the initial status of the Telegram client (connected, needs auth code, etc.)
    and pre-fills UI fields with environment variable values.
    """
    current_phone_number_display = PHONE_NUMBER + " (from env var)" if PHONE_NUMBER else "Not set" 
    
    try:
        client = get_telegram_client()
        with _client_lock:
            # Ensure client is connected when status is checked
            if not client.is_connected():
                client.connect()
            
            connected = client.is_user_authorized()
            needs_auth_code = False
            
            if connected:
                # Get the authenticated user's details to retrieve phone number
                me_obj = client.get_me()
                if me_obj and me_obj.phone:
                    current_phone_number_display = f"+{me_obj.phone}" 
                else:
                    current_phone_number_display = PHONE_NUMBER + " (from env var, not found in profile)" 
            elif not STRING_SESSION_ENV: 
                # If not connected and no string_session was initially provided, assume needs initial auth
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
def send_code_route():
    """
    Handles sending the verification code to the user's phone.
    Uses environment variables for API credentials and phone.
    """
    if not API_ID or not API_HASH or not PHONE_NUMBER:
        return jsonify({"error": "TELETHON_API_ID, TELETHON_API_HASH, and TELETHON_PHONE_NUMBER environment variables must be set."}), 400

    try:
        client = get_telegram_client()
        with _client_lock:
            if not client.is_connected():
                client.connect()
            if not client.is_user_authorized():
                client.send_code_request(PHONE_NUMBER)
                return jsonify({"message": "Verification code sent. Please enter it."})
            else:
                return jsonify({"message": "Already authorized."})
    except PhoneNumberInvalidError:
        return jsonify({"error": "The phone number from TELETHON_PHONE_NUMBER env var is invalid. Please check it."}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/sign_in', methods=['POST'])
def sign_in_route():
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
        client = get_telegram_client()
        with _client_lock:
            if not client.is_connected(): 
                client.connect()
            client.sign_in(PHONE_NUMBER, phone_code)
            string_session = client.session.save()
            
            global STRING_SESSION_ENV
            STRING_SESSION_ENV = string_session 

            print("\n--- NEWLY GENERATED TELETHON STRING SESSION (COPY THIS!) ---")
            print(string_session)
            print("-----------------------------------------------------------\n")
            print("IMPORTANT: Update the 'TELETHON_STRING_SESSION' environment variable on Render with the string above for future persistent logins.")
            
            me_obj = client.get_me()
            current_phone_number_display = f"+{me_obj.phone}" if me_obj and me_obj.phone else PHONE_NUMBER + " (from env var, not found in profile)"

            return jsonify({"message": "Signed in successfully! Session printed to logs. Please update Render ENV var.", "phone_number": current_phone_number_display})
    except SessionPasswordNeededError:
        return jsonify({"error": "Two-factor authentication is enabled. This app does not support it currently. Please disable 2FA or use a session string from a 2FA-handled login."}), 403
    except Exception as e:
        traceback.print_exc()
        global _telegram_client
        if _telegram_client:
            _telegram_client.disconnect()
            _telegram_client = None # Reset client on failure to force re-initialization
        return jsonify({"error": str(e)}), 500


@app.route('/get_groups', methods=['POST'])
def get_groups_route():
    """Lists available megagroups for the authenticated user."""
    try:
        client = get_telegram_client()
        with _client_lock:
            if not client.is_connected() or not client.is_user_authorized():
                return jsonify({"error": "Telegram client not connected or authorized. Please connect/authenticate."}), 401

            result = client(GetDialogsRequest(
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
def list_members_route():
    """Lists members of a selected group and returns them."""
    data = request.json
    group_id_str = data.get('group_id')
    # group_hash_str is no longer strictly needed for get_input_entity with just the ID
    # but keeping it in the frontend payload doesn't hurt.

    if not group_id_str: 
        return jsonify({"error": "Missing group ID"}), 400

    try:
        client = get_telegram_client()
        with _client_lock: # Ensure the global client is locked during operations
            if not client.is_connected() or not client.is_user_authorized():
                return jsonify({"error": "Telegram client not connected or authorized. Please connect/authenticate."}), 401

            try:
                group_id_int = int(group_id_str)
                # Use client.get_input_entity() which is the most robust way to get an InputPeer from an ID
                target_group_entity = client.get_input_entity(group_id_int)
                
            except ValueError:
                return jsonify({"error": f"Invalid group ID format. ID: '{group_id_str}'"}), 400
            except Exception as e:
                traceback.print_exc()
                return jsonify({"error": f"Failed to resolve group entity (ID: '{group_id_str}'). Ensure it's a valid group you have access to. Detail: {str(e)}"}), 400

            participants = client.get_participants(target_group_entity, aggressive=True)
            
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
        # Before starting the thread, quickly check if the main client is authorized
        # The thread will then create its own client
        client = get_telegram_client()
        with _client_lock:
            if not client.is_connected() or not client.is_user_authorized():
                return jsonify({"error": "Telegram client not connected or authorized. Please connect/authenticate."}), 401

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
        # Pass necessary env vars to the thread function
        # The thread will create its OWN client instance
        add_thread = threading.Thread(target=_add_members_threaded, args=(
            API_ID, 
            API_HASH, 
            STRING_SESSION_ENV, 
            PHONE_NUMBER,
            int(group_id_str), # Pass as int directly
            int(group_hash_str), # Pass as int directly
            users_to_add, 
            int(add_method)
        ))
        add_thread.start()

        return jsonify({"message": f"Adding members initiated for {len(users_to_add)} users. Progress will be logged on the server. Please allow time for completion due to Telegram's rate limits (60s per user)."}), 202

    except Exception as e: 
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# --- Function to run in the separate thread ---
def _add_members_threaded(api_id, api_hash, string_session_env, phone_number, group_id_int, group_hash_int, users_list, add_method):
    """
    This function runs in a separate thread and manages its own TelegramClient instance.
    """
    thread_client = None
    try:
        # Create a new TelegramClient instance for this thread
        print(f"THREAD DEBUG: Initializing client in new thread. API_ID: {api_id}")
        session_obj = StringSession(string_session_env) if string_session_env else StringSession(None)
        thread_client = TelegramClient(session_obj, int(api_id), api_hash)
        
        # Connect and ensure authorization within this thread
        thread_client.connect()
        if not thread_client.is_user_authorized():
            # This case means the string_session_env was not valid for the thread's client.
            # In a real scenario, you'd need a robust re-auth mechanism here,
            # but for this app, we rely on main client being authorized.
            # If it gets here, it means the session from env var is bad for the new client.
            print("THREAD ERROR: Client in new thread is not authorized. Stopping add operation.")
            return # Exit thread

        print(f"THREAD DEBUG: Client connected and authorized in thread for group ID: {group_id_int}")
        
        # Resolve the target group entity within this thread's client context
        target_group_entity = thread_client.get_input_entity(group_id_int)
        
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
                    # Use thread_client for get_input_entity
                    user_entity = thread_client.get_input_entity(user['username']) 
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
                    # Use thread_client for InviteToChannelRequest
                    thread_client(InviteToChannelRequest(target_group_entity, [user_entity]))
                    added_count += 1
                    print(f'Successfully added {user["username"] or user["id"]}. Waiting 60 seconds...')
                    time.sleep(60) # Telegram rate limit delay
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
            thread_client.disconnect()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
