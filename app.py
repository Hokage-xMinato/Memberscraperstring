import os
from flask import Flask, request, jsonify, send_from_directory
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty, InputPeerChannel, InputPeerUser, User, Channel, Chat
from telethon.errors.rpcerrorlist import PeerFloodError, UserPrivacyRestrictedError, SessionPasswordNeededError, PhoneNumberInvalidError, UserNotMutualContactError, UserAlreadyParticipantError, UserIdInvalidError
from telethon.tl.functions import users
from telethon.tl.functions.channels import InviteToChannelRequest
import csv
import io
import traceback
import time
import re
import threading
import json
import asyncio
import functools

app = Flask(__name__, static_folder='static', static_url_path='')

# Global client credentials (read once from env vars)
API_ID = os.environ.get('TELETHON_API_ID')
API_HASH = os.environ.get('TELETHON_API_HASH')
PHONE_NUMBER = os.environ.get('TELETHON_PHONE_NUMBER')
STRING_SESSION_ENV = os.environ.get('TELETHON_STRING_SESSION')

# Decorator to handle running async Flask routes
def run_async(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper

async def get_telegram_client_per_request_async():
    """
    Creates, connects, and authorizes a NEW TelegramClient instance for a single request.
    This client is local to the async function that calls it.
    """
    if not API_ID or not API_HASH:
        raise ValueError("TELETHON_API_ID and TELETHON_API_HASH environment variables must be set.")
    if not PHONE_NUMBER:
        raise ValueError("TELETHON_PHONE_NUMBER environment variable must be set (required for re-authentication).")
    
    session_obj = StringSession(STRING_SESSION_ENV) if STRING_SESSION_ENV else StringSession(None)
    client = TelegramClient(session_obj, int(API_ID), API_HASH)
    
    print("DEBUG: Connecting Telegram client for this request...")
    await client.connect()
    print("DEBUG: Telegram client connected for this request.")

    return client

@app.route('/')
def serve_index():
    """Serves the index.html file from the static folder."""
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/get_initial_status', methods=['GET', 'POST'])
@run_async
async def get_initial_status():
    """
    Returns the initial status of the Telegram client (connected, needs auth code, etc.)
    and pre-fills UI fields with environment variable values.
    """
    current_phone_number_display = PHONE_NUMBER + " (from env var)" if PHONE_NUMBER else "Not set"
    client = None
    try:
        client = await get_telegram_client_per_request_async()
        
        connected = await client.is_user_authorized()
        needs_auth_code = False
        
        if connected:
            me_obj = await client.get_me()
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
    finally:
        if client and client.is_connected():
            print("DEBUG: Disconnecting client for get_initial_status request.")
            await client.disconnect()


@app.route('/send_code', methods=['POST'])
@run_async
async def send_code_route():
    """
    Handles sending the verification code to the user's phone.
    Uses environment variables for API credentials and phone.
    """
    if not API_ID or not API_HASH or not PHONE_NUMBER:
        return jsonify({"error": "TELETHON_API_ID, TELETHON_API_HASH, and TELETHON_PHONE_NUMBER environment variables must be set."}), 400

    client = None
    try:
        client = await get_telegram_client_per_request_async()
        
        if not await client.is_user_authorized():
            await client.send_code_request(PHONE_NUMBER)
            return jsonify({"message": "Verification code sent. Please enter it."})
        else:
            return jsonify({"message": "Already authorized."})
    except PhoneNumberInvalidError:
        return jsonify({"error": "The phone number from TELETHON_PHONE_NUMBER env var is invalid. Please check it."}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if client and client.is_connected():
            print("DEBUG: Disconnecting client for send_code_route request.")
            await client.disconnect()


@app.route('/sign_in', methods=['POST'])
@run_async
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

    client = None
    try:
        client = await get_telegram_client_per_request_async()
        
        await client.sign_in(PHONE_NUMBER, phone_code)
        string_session = client.session.save()
        
        global STRING_SESSION_ENV
        STRING_SESSION_ENV = string_session # Update the global env var for next connections

        print("\n--- NEWLY GENERATED TELETHON STRING SESSION (COPY THIS!) ---")
        print(string_session)
        print("-----------------------------------------------------------\n")
        print("IMPORTANT: Update the 'TELETHON_STRING_SESSION' environment variable on Render with the string above for future persistent logins.")
        
        me_obj = await client.get_me()
        current_phone_number_display = f"+{me_obj.phone}" if me_obj and me_obj.phone else PHONE_NUMBER + " (from env var, not found in profile)"

        return jsonify({"message": "Signed in successfully! Session printed to logs. Please update Render ENV var.", "phone_number": current_phone_number_display})
    except SessionPasswordNeededError:
        return jsonify({"error": "Two-factor authentication is enabled. This app does not support it currently. Please disable 2FA or use a session string from a 2FA-handled login."}), 403
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if client and client.is_connected():
            print("DEBUG: Disconnecting client for sign_in_route request.")
            await client.disconnect()


@app.route('/get_groups', methods=['POST'])
@run_async
async def get_groups_route():
    """Lists available megagroups for the authenticated user."""
    client = None
    try:
        client = await get_telegram_client_per_request_async()
        
        if not await client.is_user_authorized():
            return jsonify({"error": "Telegram client not connected or authorized. Please connect/authenticate."}), 401

        result = await client(GetDialogsRequest(
            offset_date=None,
            offset_id=0,
            offset_peer=InputPeerEmpty(),
            limit=200,
            hash=0
        ))
        # Filter to include only megagroups (supergroups)
        groups = []
        for chat in result.chats:
            # Telethon returns different types of chat objects (Chat, Channel, User)
            # Megagroups are instances of Channel
            if isinstance(chat, Channel) and chat.megagroup:
                groups.append({
                    'id': chat.id,
                    'title': chat.title,
                    'access_hash': chat.access_hash
                })
        
        return jsonify({"groups": groups})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if client and client.is_connected():
            print("DEBUG: Disconnecting client for get_groups_route request.")
            await client.disconnect()

@app.route('/list_members', methods=['POST'])
@run_async
async def list_members_route():
    """Lists members of a selected group and returns them."""
    data = request.json
    group_id_str = data.get('group_id')
    group_hash_str = data.get('group_hash')

    if not all([group_id_str, group_hash_str]):
        return jsonify({"error": "Missing group ID or hash"}), 400

    client = None
    try:
        client = await get_telegram_client_per_request_async()
        
        if not await client.is_user_authorized():
            return jsonify({"error": "Telegram client not connected or authorized. Please connect/authenticate."}), 401

        try:
            group_id_int = int(group_id_str)
            group_hash_int = int(group_hash_str)
            
            # Resolve the full entity object using client.get_entity()
            target_group_entity = await client.get_entity(InputPeerChannel(group_id_int, group_hash_int))
            
        except ValueError:
            return jsonify({"error": f"Invalid group ID or hash format. ID: '{group_id_str}', Hash: '{group_hash_str}'"}), 400
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": f"Failed to resolve group entity (ID: '{group_id_str}', Hash: '{group_hash_str}'). Ensure it's a valid group you have access to. Detail: {str(e)}"}), 500

        participants = await client.get_participants(target_group_entity, aggressive=True)
        
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
    finally:
        if client and client.is_connected():
            print("DEBUG: Disconnecting client for list_members_route request.")
            await client.disconnect()

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
        if not API_ID or not API_HASH or not STRING_SESSION_ENV or not PHONE_NUMBER:
            return jsonify({"error": "Missing Telegram API credentials (ID, Hash, String Session, Phone Number) in environment variables. Please set them on Render."}), 401

        users_to_add = []
        csv_file = io.StringIO(csv_data)
        reader = csv.reader(csv_file, delimiter=',', lineterminator='\n')
        next(reader, None) # Safely skip header row

        row_num = 1 # Start from 1 for header, 2 for first data row
        for row in reader:
            row_num += 1
            if len(row) < 4:
                print(f'WARNING: Skipping incomplete user record on row {row_num} (expected 4 columns, got {len(row)}): {row}')
                continue
            
            username = row[0].strip() if row[0] else ''
            
            user_id = 0
            try:
                user_id = int(row[1].strip())
            except ValueError:
                print(f'WARNING: Could not parse user_id "{row[1].strip()}" on row {row_num} as an integer. Setting to 0. Row: {row}')
                # Continue with user_id 0, Telethon will likely error if this is a required field.
                
            access_hash = 0
            try:
                # access_hash can sometimes be empty, treat as 0 in that case,
                # otherwise attempt conversion.
                # If it's a string like "None", it might still fail int().
                if row[2].strip(): # Only try to convert if not empty string
                    access_hash = int(row[2].strip())
            except ValueError:
                print(f'WARNING: Could not parse access_hash "{row[2].strip()}" on row {row_num} as an integer. Setting to 0. Row: {row}')
                # Continue with access_hash 0. Telethon might throw UserIdInvalidError later.
            
            name = row[3].strip() if row[3] else ''

            users_to_add.append({
                'username': username,
                'id': user_id,
                'access_hash': access_hash,
                'name': name
            })

        add_thread = threading.Thread(target=lambda: asyncio.run(_add_members_threaded_async(
            API_ID,
            API_HASH,
            STRING_SESSION_ENV,
            int(group_id_str),
            int(group_hash_str),
            users_to_add,
            int(add_method)
        )))
        add_thread.start()

        return jsonify({"message": f"Adding members initiated for {len(users_to_add)} users. Progress will be logged on the server. Please allow time for completion due to Telegram's rate limits (75s per user)."}), 202

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

async def _add_members_threaded_async(api_id, api_hash, string_session_env, group_id_int, group_hash_int, users_list, add_method):
    """
    This asynchronous function runs in a separate thread,
    creates its own TelegramClient instance, and manages its own event loop.
    """
    thread_client = None
    added_count = 0
    skipped_count = 0
    errors = []
    
    flood_retry_count = 0
    max_flood_retries = 3 
    base_flood_wait_time_seconds = 3600 # 1 hour, used if Telethon's error doesn't specify seconds

    try: # Outer try block for the entire async function
        print(f"THREAD DEBUG: Initializing async client in new thread. API_ID: {api_id}")
        session_obj = StringSession(string_session_env) if string_session_env else StringSession(None)
        thread_client = TelegramClient(session_obj, int(api_id), api_hash)
        
        await thread_client.connect()
        if not await thread_client.is_user_authorized():
            print("THREAD ERROR: Client in new thread is not authorized. Stopping add operation.")
            errors.append("Client in adding thread not authorized. Please re-authenticate via the UI.")
            final_message_remaining_users = len(users_list) - 0 
            final_message_str = (f'CRITICAL FAILURE: Addition interrupted early due to authorization. '
                                 f'Added: {added_count}, Skipped: {skipped_count}. '
                                 f'Users remaining: {final_message_remaining_users}. '
                                 f'Errors: {"; ".join(errors)}.')
            print(final_message_str)
            return

        print(f"THREAD DEBUG: Async client connected and authorized in thread for group ID: {group_id_int}")
        
        target_group_entity = await thread_client.get_entity(InputPeerChannel(group_id_int, group_hash_int))
        
        print(f"THREAD DEBUG: Fetching existing members for group ID: {group_id_int}...")
        existing_member_ids = set()
        existing_member_usernames = set()
        
        try:
            current_participants = await thread_client.get_participants(target_group_entity, aggressive=True)
            for p in current_participants:
                existing_member_ids.add(p.id)
                if p.username:
                    existing_member_usernames.add(p.username.lower()) 
            print(f"THREAD DEBUG: Found {len(existing_member_ids)} existing members.")
        except Exception as e:
            print(f"THREAD WARNING: Could not fetch existing members for pre-check due to: {e}. Some 'UserAlreadyParticipantError' might still occur.")

        current_user_index = 0
        while current_user_index < len(users_list):
            user = users_list[current_user_index]
            
            # Check if user is already a participant (more robust than just ID)
            is_already_member = False
            if user['id'] and user['id'] in existing_member_ids:
                is_already_member = True
            elif user['username'] and user['username'].lower() in existing_member_usernames:
                is_already_member = True

            if is_already_member:
                skipped_count += 1
                errors.append(f'Skipped {user["username"] or user["id"]}: Already in group (pre-check).')
                print(f'Skipped {user["username"] or user["id"]}: Already in group (pre-check).')
                current_user_index += 1 
                continue 
            
            try: # Inner try block for individual user addition attempt
                print(f'Attempting to add {user["username"] or user["id"]} (User {current_user_index + 1}/{len(users_list)})')
                user_entity = None
                
                if add_method == 1:  # by username
                    if not user['username']:
                        errors.append(f'Skipped user ID {user["id"]}: Missing username for "by username" method.')
                        skipped_count += 1
                        current_user_index += 1
                        continue 
                    user_entity = await thread_client.get_input_entity(user['username']) 
                elif add_method == 2:  # by ID
                    if not user['id'] or user['access_hash'] is None: 
                        errors.append(f'Skipped user {user["username"] or user["id"]}: Missing User ID or Access Hash for "by ID" method.')
                        skipped_count += 1
                        current_user_index += 1
                        continue
                    # Telethon expects int for user ID and access hash
                    user_entity = InputPeerUser(int(user['id']), int(user['access_hash']))
                else: 
                    errors.append(f'Skipped user {user["username"] or user["id"]}: Invalid add method specified.')
                    break # Exit while loop entirely

                # If we got a user_entity, attempt to invite
                await thread_client(InviteToChannelRequest(target_group_entity, [user_entity])) 
                added_count += 1
                print(f'Successfully added {user["username"] or user["id"]}. Waiting 75 seconds...') 
                await asyncio.sleep(75) 
                flood_retry_count = 0 
                current_user_index += 1 # Advance for successful add

            except PeerFloodError as e:
                flood_retry_count += 1
                wait_time = getattr(e, 'seconds', base_flood_wait_time_seconds * (2 ** (flood_retry_count - 1)))
                wait_time = min(wait_time, 24 * 3600) # Cap at 24 hours
                
                error_msg = (f'PeerFloodError for {user["username"] or user["id"]}: Too many requests. '
                             f'Telegram asks to wait {wait_time/3600:.1f} hours ({wait_time} seconds) before retrying. '
                             f'(Retry {flood_retry_count}/{max_flood_retries}).')
                errors.append(error_msg)
                print(error_msg)
                if flood_retry_count <= max_flood_retries:
                    print(f"RECOMMENDATION: Your account is likely flood-limited. Consider pausing for {wait_time/3600:.1f} hours, or using a different account for future operations. The script will wait, but this is a Telegram restriction.")
                    await asyncio.sleep(wait_time)
                    # current_user_index is NOT incremented here, we retry the same user
                else:
                    errors.append(f'PeerFloodError: Maximum retries ({max_flood_retries}) exceeded for {user["username"] or user["id"]}. Stopping addition.')
                    print(f'CRITICAL: Max flood retries exceeded. Stopping add operation at user {current_user_index + 1}.')
                    break 
            except (UserPrivacyRestrictedError, UserNotMutualContactError, UserAlreadyParticipantError, UserIdInvalidError) as e: 
                skipped_count += 1
                errors.append(f'Skipped {user["username"] or user["id"]}: {e.__class__.__name__} - {str(e)}')
                print(f'Skipped {user["username"] or user["id"]}: {e.__class__.__name__} - {str(e)}')
                current_user_index += 1 
                flood_retry_count = 0 
            except Exception as e: 
                skipped_count += 1
                errors.append(f'Error adding {user["username"] or user["id"]}: {e.__class__.__name__} - {str(e)}')
                traceback.print_exc()
                print(f'Unexpected error adding {user["username"] or user["id"]}: {e.__class__.__name__} - {str(e)}')
                current_user_index += 1 
                flood_retry_count = 0 

    final_message_remaining_users = len(users_list) - current_user_index
    final_message_str = (f'Finished adding members. '
                         f'Added: {added_count}, Skipped: {skipped_count}. '
                         f'Total users processed: {len(users_list)}. '
                         f'Users remaining due to interruption: {final_message_remaining_users}. '
                         f'Errors: {"; ".join(errors) if errors else "None."}')
    print(final_message_str)

    except Exception as e: 
        print(f"THREAD CRITICAL ERROR: An unexpected error occurred in the adding thread: {e}")
        traceback.print_exc()
        errors.append(f"CRITICAL THREAD ERROR: {e.__class__.__name__} - {str(e)}") 
        current_user_index_at_crash = locals().get('current_user_index', 0) 
        final_message_remaining_users = len(users_list) - current_user_index_at_crash
        final_message_str = (f'CRITICAL FAILURE: Addition interrupted unexpectedly. '
                             f'Added: {added_count}, Skipped: {skipped_count}. '
                             f'Users remaining (at crash): {final_message_remaining_users}. '
                             f'Errors: {"; ".join(errors)}.')
        print(final_message_str)
    finally: 
        if thread_client and thread_client.is_connected():
            print("THREAD DEBUG: Disconnecting client in thread.")
            await thread_client.disconnect()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
