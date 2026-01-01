
import pyrogram.utils
from threading import Thread
import time, os
def fixed_get_peer_type(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    else:
        return "chat"

pyrogram.utils.get_peer_type = fixed_get_peer_type

import logging
import asyncio
from functools import partial
import re
from urllib.parse import urlparse
from collections import defaultdict
import json
from flask import Flask, request, jsonify
import requests
from pyrogram import Client, filters, types, idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo




API_ID = 21001953
API_HASH = "8c8549fb2be0f9c6bcc917b449e52e3b"
BOT_TOKEN = "8035704566:AAHUPZ5784tBKqpn9yryCJ3AXbZMnZh7vwQ"

FORWARD_DATA_URL = "https://core.xydevs.com/apiv1/telegram/selfbot/xyhelper/get-forward-data"
FORWARD_MODIFY_URL = "https://core.xydevs.com/apiv1/telegram/selfbot/xyhelper/modify-forward-data"
FORWARD_DATA_HEADERS = {"Authorization": "xydevsworld"}

FORWARD_DATA_PAYLOAD = "{}"

WHITELIST_USER_IDS = [6320998144, 1238655724]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

albums = defaultdict(list)

user_media_collection = defaultdict(list)

user_processing_tasks = {}

# Track messages for auto-delete
button_messages = {}

# Track search state per user
user_search_state = {}

# Track forward data cache per user
user_forward_data_cache = {}

flask_app = Flask(__name__)
app = Client(
    "xylence_helper",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

def get_msg_id(msg):
    """Return message id for a pyrogram Message, handling different attribute names."""
    if not msg:
        return None
    return getattr(msg, "message_id", getattr(msg, "id", None))

def reconstruct_chat_id(chatid_no_minus: str) -> int:
    """Reconstruct full chat id from the 'no-minus' value used in t.me/c links or API responses.
    If the value already starts with '100' we convert to '-{value}', else we prefix '-100{value}'."""
    s = str(chatid_no_minus)
    if s.startswith("100"):
        return int("-" + s)
    return int("-100" + s)

def resolve_chat_identifier(chat_safe: str):
    """Return a value suitable for pyrogram chat_id param. If numeric, reconstruct to signed id; otherwise return username/identifier (with @ if missing)."""
    s = str(chat_safe)

    if s.lstrip("-").isdigit():
        return reconstruct_chat_id(s)

    if s.startswith("@"):
        return s

    return "@" + s

def parse_value(value: str, fallback_msg: str | None = None) -> dict:
    """Parse a 'value' field from backend which may be a chat id, a t.me url, or other.
    Returns dict with keys: chat (string), msg (string), thread_id (int|None), is_url (bool), raw (original value).
    """
    v = (value or "").strip()
    result = {"chat": None, "msg": None, "thread_id": None, "is_url": False, "raw": v}

    if not v:
        result["msg"] = fallback_msg
        return result

    if "t.me" in v:
        result["is_url"] = True
        try:
            p = urlparse(v)
            parts = [part for part in p.path.split("/") if part]

            if parts and parts[0] == "c" and len(parts) >= 3:

                result["chat"] = parts[1]
                if len(parts) == 3:
                    result["msg"] = parts[2]
                elif len(parts) >= 4:

                    result["thread_id"] = int(parts[2]) if parts[2].isdigit() else None
                    result["msg"] = parts[3]
            elif len(parts) >= 2 and parts[1].isdigit():

                result["chat"] = parts[0]
                result["msg"] = parts[1]
            else:

                digits = [seg for seg in parts if seg.isdigit()]
                if digits:
                    result["chat"] = digits[0]
                    result["msg"] = digits[-1] if len(digits) > 1 else fallback_msg
                else:
                    result["chat"] = parts[0]
                    result["msg"] = fallback_msg
        except Exception:
            result["chat"] = v
            result["msg"] = fallback_msg
        return result

    stripped = v.lstrip("-")
    if stripped.isdigit():
        result["chat"] = stripped
        result["msg"] = fallback_msg
        return result

    result["chat"] = v
    result["msg"] = fallback_msg
    return result

def filter_url_markup(reply_markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Return an InlineKeyboardMarkup that contains only url buttons (preserving layout rows when possible).
    If there is no url button, returns None."""
    if not reply_markup:
        return None

    new_rows = []
    for row in reply_markup.inline_keyboard:
        new_row = []
        for btn in row:

            if getattr(btn, "url", None):
                new_row.append(InlineKeyboardButton(text=btn.text, url=btn.url))
        if new_row:
            new_rows.append(new_row)

    if not new_rows:
        return None

    return InlineKeyboardMarkup(new_rows)

async def schedule_auto_delete(message, delay=60, user_id=None):
    """Schedule a message to be deleted after delay seconds"""
    message_key = f"{message.chat.id}_{message.id}"
    
    # Cancel previous task if exists
    if message_key in button_messages:
        button_messages[message_key]['task'].cancel()
    
    async def delete_task():
        try:
            await asyncio.sleep(delay)
            await message.delete()
            button_messages.pop(message_key, None)
            # Clean up user states
            if user_id:
                user_search_state.pop(user_id, None)
                user_forward_data_cache.pop(user_id, None)
        except Exception as e:
            logger.exception(f"Error deleting message: {e}")
    
    task = asyncio.create_task(delete_task())
    button_messages[message_key] = {'task': task, 'message': message, 'user_id': user_id}

def build_paginated_keyboard(items, page=0, per_page=10, callback_prefix="", search_query=None, context_type="forward"):
    """Build a paginated keyboard with 2 columns and 5 rows (10 items per page)
    
    Args:
        items: List of items to paginate
        page: Current page (0-indexed)
        per_page: Items per page (default 10)
        callback_prefix: Prefix for callback data
        search_query: Optional search query to filter items
        context_type: Type of context (forward or settings)
    """
    # Filter items by search query if provided
    if search_query:
        filtered_items = [item for item in items if search_query.lower() in item.get("context", "").lower()]
    else:
        filtered_items = items
    
    total_items = len(filtered_items)
    total_pages = (total_items + per_page - 1) // per_page if total_items > 0 else 1
    
    # Ensure page is within bounds
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total_items)
    page_items = filtered_items[start_idx:end_idx]
    
    rows = []
    
    # Build 2-column layout (5 rows × 2 columns = 10 items)
    for i in range(0, len(page_items), 2):
        row = []
        for j in range(2):
            if i + j < len(page_items):
                item = page_items[i + j]
                context = item.get("context") or "(no context)"
                # Truncate long context
                display_text = context[:20] + "..." if len(context) > 20 else context
                
                if context_type == "forward":
                    # For forward buttons
                    value = str(item.get("value", ""))
                    msg_field = str(item.get("msg", ""))
                    parsed = parse_value(value, fallback_msg=msg_field or None)
                    chat = parsed.get("chat")
                    msg = parsed.get("msg") or msg_field or "0"
                    thread_id = parsed.get("thread_id") or 0
                    
                    if chat:
                        chat_safe = str(chat).replace("_", "-")
                        # Encode search query and page in callback
                        search_param = search_query if search_query else ""
                        cb = f"{callback_prefix}{chat_safe}_{msg}_{{source_msg_id}}_{thread_id}_{page}_{search_param}"
                        row.append(InlineKeyboardButton(text=display_text, callback_data=cb))
                else:
                    # For settings buttons
                    item_id = item.get("id")
                    cb = f"{callback_prefix}{item_id}_{page}_{search_query or ''}"
                    row.append(InlineKeyboardButton(text=display_text, callback_data=cb))
        
        if row:
            rows.append(row)
    
    # Pagination buttons (if needed)
    if total_pages > 1:
        nav_row = []
        # Previous button
        if page > 0:
            nav_row.append(InlineKeyboardButton(
                "◀️ Prev",
                callback_data=f"page_{context_type}_{page-1}_{search_query or ''}"
            ))
        
        # Page indicator
        nav_row.append(InlineKeyboardButton(
            f"{page + 1}/{total_pages}",
            callback_data="noop"
        ))
        
        # Next button
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(
                "Next ▶️",
                callback_data=f"page_{context_type}_{page+1}_{search_query or ''}"
            ))
        
        if nav_row:
            rows.append(nav_row)
    
    # Search button in its own row
    search_icon = "🔍" if not search_query else "✖️"
    search_text = "Search" if not search_query else f"Clear ({search_query})"
    rows.append([InlineKeyboardButton(
        search_icon + " " + search_text,
        callback_data=f"search_{context_type}_{page}"
    )])
    
    # Cancel button
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel")])
    
    return InlineKeyboardMarkup(rows), total_items, total_pages

async def show_forward_settings(client, message, edit_msg=None):
    """Display forward settings with inline buttons"""
    # Fetch forward data
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            partial(requests.post, FORWARD_DATA_URL, headers=FORWARD_DATA_HEADERS, data=FORWARD_DATA_PAYLOAD, timeout=10),
        )
        
        if resp.status_code != 200:
            text = "❌ Gagal mengambil data forward settings."
            if edit_msg:
                return await edit_msg.edit_text(text)
            return await message.reply_text(text)
        
        data = resp.json()
        if not data.get("success") or not data.get("data"):
            text = "📭 Belum ada forward settings yang dikonfigurasi."
        else:
            items = data.get("data", [])
            text = f"⚙️ **Forward Settings** ({len(items)} item)\n\n"
            
        # Build keyboard
        rows = []
        if data.get("success") and data.get("data"):
            for item in data.get("data", []):
                item_id = item.get("id")
                context = item.get("context") or "(no context)"
                value = item.get("value") or "(no value)"
                # Truncate long values
                display_value = value[:30] + "..." if len(value) > 30 else value
                btn_text = f"📝 {context}: {display_value}"
                rows.append([InlineKeyboardButton(btn_text, callback_data=f"admin_forward_settings_{item_id}")])
        
        # Add button
        rows.append([InlineKeyboardButton("➕ Tambah Forward Option", callback_data="admin_forward_settings_add")])
        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="admin_forward_settings_refresh")])
        
        markup = InlineKeyboardMarkup(rows)
        
        if edit_msg:
            await edit_msg.edit_text(text, reply_markup=markup)
        else:
            await message.reply_text(text, reply_markup=markup)
    
    except Exception as e:
        logger.exception("Error fetching forward settings")
        text = f"❌ Error: {str(e)}"
        if edit_msg:
            await edit_msg.edit_text(text)
        else:
            await message.reply_text(text)


@app.on_message(filters.command("start"))
async def cmd_start(client, message):
    await message.reply_text(
        "Halo! Saya bot forwarder Xydevs-Helper. Powered By Xydevs.com"
    )
  
async def fetch_forward_data():
    """Fetch forward data from the configured backend and return parsed JSON or None."""
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            partial(requests.post, FORWARD_DATA_URL, headers=FORWARD_DATA_HEADERS, data=FORWARD_DATA_PAYLOAD, timeout=10),
        )
    except Exception:
        logger.exception("Error saat meminta data forward")
        return None

    if resp.status_code != 200:
        logger.error("Bad status from forward data API: %s", resp.status_code)
        print(f"[DEBUG] fetch_forward_data - Status: {resp.status_code}")
        print(f"[DEBUG] fetch_forward_data - Response: {resp.text}")
        return None

    try:
        result = resp.json()
        print(f"[DEBUG] fetch_forward_data - Success")
        print(f"[DEBUG] fetch_forward_data - Response: {json.dumps(result, indent=2)}")
        return result
    except Exception:
        logger.exception("Gagal parsing JSON dari forward data API")
        print(f"[DEBUG] fetch_forward_data - Parse error, raw response: {resp.text}")
        return None


async def modify_forward_data(action, item_id=None, context=None, value=None):
    """Modify forward data via API (add/modify/delete)"""
    try:
        payload = {"action": action}
        if item_id:
            payload["id"] = item_id
        if context:
            payload["context"] = context
        if value:
            payload["value"] = value
        
        print(f"[DEBUG] modify_forward_data - Payload: {json.dumps(payload, indent=2)}")
        
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            partial(
                requests.post, 
                FORWARD_MODIFY_URL, 
                headers=FORWARD_DATA_HEADERS, 
                json=payload, 
                timeout=10
            ),
        )
        
        print(f"[DEBUG] modify_forward_data - Status: {resp.status_code}")
        print(f"[DEBUG] modify_forward_data - Response: {resp.text}")
        
        if resp.status_code != 200:
            return {"success": False, "message": f"HTTP {resp.status_code}"}
        
        result = resp.json()
        print(f"[DEBUG] modify_forward_data - Parsed: {json.dumps(result, indent=2)}")
        return result
    except Exception as e:
        logger.exception("Error modifying forward data")
        print(f"[DEBUG] modify_forward_data - Exception: {str(e)}")
        return {"success": False, "message": str(e)}


async def show_forward_settings(client, message_or_query, edit_mode=False, page=0, search_query=None, use_cache=False):
    """Display forward settings with inline buttons"""
    try:
        # Get user_id
        if hasattr(message_or_query, 'from_user'):
            user_id = message_or_query.from_user.id
        elif hasattr(message_or_query, 'message') and hasattr(message_or_query.message, 'chat'):
            # For callback query, get from original message
            user_id = message_or_query.from_user.id if hasattr(message_or_query, 'from_user') else None
        else:
            user_id = None
        
        # Check cache first if use_cache is True
        if use_cache and user_id and user_id in user_forward_data_cache:
            items = user_forward_data_cache[user_id]
        else:
            # Fetch forward data
            data = await fetch_forward_data()
            
            if not data or not data.get("success"):
                text = "📭 Belum ada forward settings yang dikonfigurasi."
                items = []
            else:
                items = data.get("data", [])
                # Cache the data for this user
                if user_id:
                    user_forward_data_cache[user_id] = items
        
        # Build paginated keyboard
        markup, total_items, total_pages = build_paginated_keyboard(
            items,
            page=page,
            per_page=10,
            callback_prefix="admin_forward_settings_",
            search_query=search_query,
            context_type="settings"
        )
        
        # Add management buttons at the bottom (before cancel)
        if markup.inline_keyboard:
            # Remove cancel button temporarily
            cancel_btn = markup.inline_keyboard[-1]
            management_btns = [
                [InlineKeyboardButton("➕ Tambah", callback_data=f"admin_forward_settings_add_{page}_{search_query or ''}")],
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"admin_forward_settings_refresh_{page}_{search_query or ''}")]
            ]
            # Rebuild keyboard
            new_rows = markup.inline_keyboard[:-1] + management_btns + [cancel_btn]
            markup = InlineKeyboardMarkup(new_rows)
        
        # Build text
        search_info = f" (Filtered: {total_items})" if search_query else f" ({total_items} item)"
        page_info = f" - Page {page + 1}/{total_pages}" if total_pages > 1 else ""
        text = f"⚙️ **Forward Settings**{search_info}{page_info}"
        
        # Send or edit message
        if edit_mode:
            sent_msg = await message_or_query.message.edit_text(text, reply_markup=markup)
        else:
            if hasattr(message_or_query, 'message'):  # It's a callback query
                sent_msg = await message_or_query.message.edit_text(text, reply_markup=markup)
            else:  # It's a message
                sent_msg = await message_or_query.reply_text(text, reply_markup=markup)
        
        # Schedule auto-delete with user_id
        await schedule_auto_delete(sent_msg, delay=60, user_id=user_id)
    
    except Exception as e:
        logger.exception("Error showing forward settings")
        text = f"❌ Error: {str(e)}"
        if edit_mode or hasattr(message_or_query, 'message'):
            await message_or_query.message.edit_text(text)
        else:
            await message_or_query.reply_text(text)


@app.on_message(filters.command("forward_settings"))
async def cmd_forward(client, message):
    # Check if user is whitelisted
    if not message.from_user or message.from_user.id not in WHITELIST_USER_IDS:
        return await message.reply_text("Anda tidak memiliki akses ke command ini.")
    
    # If it's a reply to message, could be implemented later for quick forward
    # For now, just show settings management
    await show_forward_settings(client, message)


@app.on_message(filters.command("forward"))
async def cmd_forward_command(client, message):
    """Forward the replied-to message using configured forward settings.
    If not used as a reply, fallback to showing the forward-settings UI."""
    # Check if user is whitelisted
    if not message.from_user or message.from_user.id not in WHITELIST_USER_IDS:
        return await message.reply_text("Anda tidak memiliki akses ke command ini.")

    # If not used as a reply, show current forward settings UI
    if not getattr(message, "reply_to_message", None):
        return await show_forward_settings(client, message)

    source_msg = message.reply_to_message
    source_msg_id = get_msg_id(source_msg)

    user_id = message.from_user.id
    
    # Fetch and cache data
    data = await fetch_forward_data()
    if not data or not data.get("data"):
        return await message.reply_text("Tidak ada data tujuan yang ditemukan.")

    items = data.get("data") or []
    # Cache for this user
    user_forward_data_cache[user_id] = items
    
    # Store source_msg_id in user state for pagination
    if user_id not in user_search_state:
        user_search_state[user_id] = {}
    user_search_state[user_id]['source_msg_id'] = source_msg_id
    
    # Build paginated keyboard
    markup, total_items, total_pages = build_paginated_keyboard(
        items,
        page=0,
        per_page=10,
        callback_prefix="admin_execute_forward2_",
        search_query=None,
        context_type="forward"
    )
    
    # Replace {source_msg_id} in callback data
    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            if hasattr(btn, 'callback_data') and btn.callback_data:
                new_cb = btn.callback_data.replace("{source_msg_id}", str(source_msg_id))
                new_row.append(InlineKeyboardButton(text=btn.text, callback_data=new_cb))
            else:
                new_row.append(btn)
        new_rows.append(new_row)
    
    markup = InlineKeyboardMarkup(new_rows)

    # Send selection as reply to the source message so callback_query.message refers to it
    selection_msg = await source_msg.reply_text("Pilih topik tujuan untuk Forward:", reply_markup=markup)
    
    # Schedule auto-delete with user_id
    await schedule_auto_delete(selection_msg, delay=60, user_id=user_id)
    
    return


@app.on_callback_query(filters.regex(r"^admin_forward_settings_refresh_(\d+)_(.*)$"))
async def on_settings_refresh(client, callback_query):
    try:
        await callback_query.answer("Memuat ulang...")
    except:
        pass
    
    m = re.match(r"^admin_forward_settings_refresh_(\d+)_(.*)$", callback_query.data)
    page = int(m.group(1))
    search_query = m.group(2) if m.group(2) else None
    
    # Clear cache for this user to force reload
    user_id = callback_query.from_user.id
    user_forward_data_cache.pop(user_id, None)
    
    await show_forward_settings(client, callback_query, edit_mode=True, page=page, search_query=search_query, use_cache=False)


@app.on_callback_query(filters.regex(r"^page_(settings|forward)_(\d+)_(.*)$"))
async def on_page_navigation(client, callback_query):
    """Handle page navigation"""
    try:
        await callback_query.answer()
    except:
        pass
    
    m = re.match(r"^page_(settings|forward)_(\d+)_(.*)$", callback_query.data)
    context_type = m.group(1)
    page = int(m.group(2))
    search_query = m.group(3) if m.group(3) else None
    
    if context_type == "settings":
        # Use cache for faster pagination
        await show_forward_settings(client, callback_query, edit_mode=True, page=page, search_query=search_query, use_cache=True)
    elif context_type == "forward":
        # Handle forward pagination with cache
        user_id = callback_query.from_user.id
        
        # Get cached data
        if user_id not in user_forward_data_cache:
            await callback_query.answer("❌ Data expired, silakan mulai ulang", show_alert=True)
            return
        
        items = user_forward_data_cache[user_id]
        
        # Build paginated keyboard
        markup, total_items, total_pages = build_paginated_keyboard(
            items,
            page=page,
            per_page=10,
            callback_prefix="admin_execute_forward2_",
            search_query=search_query,
            context_type="forward"
        )
        
        # Get source_msg_id from callback_query message (extract from existing buttons)
        # We need to preserve the source_msg_id in pagination
        # Look for it in the message text or we can store it in user state
        if user_id in user_search_state and 'source_msg_id' in user_search_state[user_id]:
            source_msg_id = user_search_state[user_id]['source_msg_id']
        else:
            # Try to extract from first button if exists
            try:
                first_btn = callback_query.message.reply_markup.inline_keyboard[0][0]
                if hasattr(first_btn, 'callback_data'):
                    # Extract source_msg_id from callback_data
                    parts = first_btn.callback_data.split('_')
                    if len(parts) >= 4:
                        source_msg_id = parts[3]
                    else:
                        await callback_query.answer("❌ Data expired, silakan mulai ulang", show_alert=True)
                        return
                else:
                    await callback_query.answer("❌ Data expired, silakan mulai ulang", show_alert=True)
                    return
            except:
                await callback_query.answer("❌ Data expired, silakan mulai ulang", show_alert=True)
                return
        
        # Replace {source_msg_id} in callback data
        new_rows = []
        for row in markup.inline_keyboard:
            new_row = []
            for btn in row:
                if hasattr(btn, 'callback_data') and btn.callback_data and '{source_msg_id}' in btn.callback_data:
                    new_cb = btn.callback_data.replace("{source_msg_id}", str(source_msg_id))
                    new_row.append(InlineKeyboardButton(text=btn.text, callback_data=new_cb))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        
        markup = InlineKeyboardMarkup(new_rows)
        
        search_info = f" (Filtered: {total_items})" if search_query else ""
        page_info = f" - Page {page + 1}/{total_pages}" if total_pages > 1 else ""
        text = f"Pilih topik tujuan untuk Forward:{search_info}{page_info}"
        
        await callback_query.message.edit_text(text, reply_markup=markup)
        
        # Reset auto-delete timer
        await schedule_auto_delete(callback_query.message, delay=60, user_id=user_id)


@app.on_callback_query(filters.regex(r"^search_(settings|forward)_(\d+)$"))
async def on_search(client, callback_query):
    """Handle search button click"""
    try:
        await callback_query.answer()
    except:
        pass
    
    # Reset auto-delete timer
    await schedule_auto_delete(callback_query.message, delay=60, user_id=callback_query.from_user.id)
    
    m = re.match(r"^search_(settings|forward)_(\d+)$", callback_query.data)
    context_type = m.group(1)
    page = int(m.group(2))
    
    user_id = callback_query.from_user.id
    
    # Check if there's an active search - if yes, clear it
    if user_id in user_search_state and user_search_state[user_id].get('query'):
        # Clear search
        user_search_state.pop(user_id, None)
        if context_type == "settings":
            await show_forward_settings(client, callback_query, edit_mode=True, page=0, search_query=None, use_cache=True)
        return
    
    # Start search
    chat = callback_query.message.chat
    
    try:
        # Store search state
        user_search_state[user_id] = {
            'context_type': context_type,
            'page': page,
            'waiting': True
        }
        
        # Ask for search input using pyromod
        search_msg = await callback_query.message.chat.ask(
            "🔍 **Search Mode**\n\n"
            "Masukkan kata kunci untuk mencari forward option:\n\n"
            "Kirim /cancel untuk membatalkan.",
            timeout=30
        )
        
        if search_msg.text and search_msg.text.startswith("/cancel"):
            user_search_state.pop(user_id, None)
            if context_type == "settings":
                return await show_forward_settings(client, callback_query, edit_mode=True, page=page)
            return
        
        search_query = search_msg.text.strip()
        
        # Delete user's search message
        try:
            await search_msg.delete()
        except:
            pass
        
        # Update search state
        user_search_state[user_id] = {
            'context_type': context_type,
            'query': search_query,
            'page': 0
        }
        
        # Show filtered results
        if context_type == "settings":
            await show_forward_settings(client, callback_query, edit_mode=True, page=0, search_query=search_query, use_cache=True)
        elif context_type == "forward":
            # Handle forward search with cache
            if user_id not in user_forward_data_cache:
                await callback_query.message.edit_text("❌ Data expired, silakan mulai ulang")
                return
            
            items = user_forward_data_cache[user_id]
            
            # Build paginated keyboard with search
            markup, total_items, total_pages = build_paginated_keyboard(
                items,
                page=0,
                per_page=10,
                callback_prefix="admin_execute_forward2_",
                search_query=search_query,
                context_type="forward"
            )
            
            # Get source_msg_id from user state
            source_msg_id = user_search_state[user_id].get('source_msg_id')
            if not source_msg_id:
                await callback_query.message.edit_text("❌ Data expired, silakan mulai ulang")
                return
            
            # Replace {source_msg_id} in callback data
            new_rows = []
            for row in markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if hasattr(btn, 'callback_data') and btn.callback_data and '{source_msg_id}' in btn.callback_data:
                        new_cb = btn.callback_data.replace("{source_msg_id}", str(source_msg_id))
                        new_row.append(InlineKeyboardButton(text=btn.text, callback_data=new_cb))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
            
            markup = InlineKeyboardMarkup(new_rows)
            
            search_info = f" (Filtered: {total_items})"
            page_info = f" - Page 1/{total_pages}" if total_pages > 1 else ""
            text = f"Pilih topik tujuan untuk Forward:{search_info}{page_info}"
            
            await callback_query.message.edit_text(text, reply_markup=markup)
        
    except asyncio.TimeoutError:
        user_search_state.pop(user_id, None)
        await callback_query.message.edit_text("❌ Timeout - search dibatalkan.")
    except Exception as e:
        logger.exception("Error in search")
        user_search_state.pop(user_id, None)
        await callback_query.message.edit_text(f"❌ Error: {str(e)}")


@app.on_callback_query(filters.regex(r"^noop$"))
async def on_noop(client, callback_query):
    """No operation - just answer the callback"""
    try:
        await callback_query.answer()
    except:
        pass


@app.on_callback_query(filters.regex(r"^admin_forward_settings_add_(\d+)_(.*)$"))
async def on_settings_add(client, callback_query):
    try:
        await callback_query.answer("Memulai proses tambah data...")
    except:
        pass
    
    m = re.match(r"^admin_forward_settings_add_(\d+)_(.*)$", callback_query.data)
    page = int(m.group(1))
    search_query = m.group(2) if m.group(2) else None
    
    chat = callback_query.message.chat
    
    try:
        # Ask for context
        context_msg = await chat.ask(
            "📝 Masukkan **Context** (nama/judul untuk forward option):\n\n"
            "Contoh: `Topic General`, `Channel News`\n\n"
            "Kirim /cancel untuk membatalkan.",
            timeout=60
        )
        
        if context_msg.text and context_msg.text.startswith("/cancel"):
            return await callback_query.message.edit_text("❌ Dibatalkan.")
        
        context = context_msg.text.strip()
        if not context:
            return await callback_query.message.edit_text("❌ Context tidak boleh kosong.")
        
        # Ask for value
        value_msg = await chat.ask(
            f"📝 Context: `{context}`\n\n"
            f"Sekarang masukkan **Value** (chat ID atau URL):\n\n"
            f"Contoh:\n"
            f"• `-1001234567890`\n"
            f"• `https://t.me/c/1234567890/5`\n\n"
            f"Kirim /cancel untuk membatalkan.",
            timeout=60
        )
        
        if value_msg.text and value_msg.text.startswith("/cancel"):
            return await callback_query.message.edit_text("❌ Dibatalkan.")
        
        value = value_msg.text.strip()
        if not value:
            return await callback_query.message.edit_text("❌ Value tidak boleh kosong.")
        await callback_query.message.edit_text("⏳ Menyimpan...")
        result = await modify_forward_data("add", context=context, value=value)
        
        if result.get("success"):
            await callback_query.message.edit_text(
                f"✅ Forward option berhasil ditambahkan!\\n\\n"
                f"📝 Context: `{context}`\\n"
                f"🔗 Value: `{value}`"
            )
            await asyncio.sleep(2)
            await show_forward_settings(client, callback_query, edit_mode=True, page=page, search_query=search_query)
        else:
            await callback_query.message.edit_text(f"❌ Gagal menambahkan: {result.get('message', 'Unknown error')}")
    
    except asyncio.TimeoutError:
        await callback_query.message.edit_text("❌ Timeout - tidak ada respon dalam 60 detik.")
    except Exception as e:
        logger.exception("Error in add forward setting")
        await callback_query.message.edit_text(f"❌ Error: {str(e)}")


@app.on_callback_query(filters.regex(r"^admin_forward_settings_(\d+)_(\d+)_(.*)$"))
async def on_settings_item(client, callback_query):
    try:
        await callback_query.answer()
    except:
        pass
    
    # Reset auto-delete timer
    user_id = callback_query.from_user.id
    await schedule_auto_delete(callback_query.message, delay=60, user_id=user_id)
    
    m = re.match(r"^admin_forward_settings_(\d+)_(\d+)_(.*)$", callback_query.data)
    item_id = int(m.group(1))
    page = int(m.group(2))
    search_query = m.group(3) if m.group(3) else None
    
    # Use cached data
    if user_id in user_forward_data_cache:
        data = {"success": True, "data": user_forward_data_cache[user_id]}
    else:
        data = await fetch_forward_data()
        if data and data.get("success"):
            user_forward_data_cache[user_id] = data.get("data", [])
    if not data or not data.get("success"):
        return await callback_query.answer("❌ Gagal mengambil data", show_alert=True)
    
    item = None
    for i in data.get("data", []):
        if i.get("id") == item_id:
            item = i
            break
    
    if not item:
        return await callback_query.answer("❌ Item tidak ditemukan", show_alert=True)
    
    context = item.get("context") or "(no context)"
    value = item.get("value") or "(no value)"
    text = (
        f"**Forward Option #{item_id}**\n\n"
        f"📝 Context: `{context}`\n"
        f"🔗 Value: `{value}`\n\n"
        f"Pilih aksi:"
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Context", callback_data=f"admin_forward_edit_context_{item_id}_{page}_{search_query or ''}")],
        [InlineKeyboardButton("🔗 Edit Value", callback_data=f"admin_forward_edit_value_{item_id}_{page}_{search_query or ''}")],
        [InlineKeyboardButton("🗑 Hapus", callback_data=f"admin_forward_delete_{item_id}_{page}_{search_query or ''}")],
        [InlineKeyboardButton("◀️ Kembali", callback_data=f"admin_forward_settings_refresh_{page}_{search_query or ''}")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^admin_forward_edit_context_(\d+)_(\d+)_(.*)$"))
async def on_edit_context(client, callback_query):
    try:
        await callback_query.answer("Edit context...")
    except:
        pass
    
    m = re.match(r"^admin_forward_edit_context_(\d+)_(\d+)_(.*)$", callback_query.data)
    item_id = int(m.group(1))
    page = int(m.group(2))
    search_query = m.group(3) if m.group(3) else None
    
    chat = callback_query.message.chat
    
    try:
        msg = await chat.ask(
            f"✏️ Masukkan **Context** baru untuk item #{item_id}:\n\n"
            f"Kirim /cancel untuk membatalkan.",
            timeout=60
        )
        
        if msg.text and msg.text.startswith("/cancel"):
            return await callback_query.message.edit_text("❌ Dibatalkan.")
        
        new_context = msg.text.strip()
        if not new_context:
            return await callback_query.message.edit_text("❌ Context tidak boleh kosong.")
        
        await callback_query.message.edit_text("⏳ Menyimpan...")
        result = await modify_forward_data("modify", item_id=item_id, context=new_context)
        
        if result.get("success"):
            await callback_query.message.edit_text(f"✅ Context berhasil diubah menjadi: `{new_context}`")
            await asyncio.sleep(2)
            await show_forward_settings(client, callback_query, edit_mode=True, page=page, search_query=search_query)
        else:
            await callback_query.message.edit_text(f"❌ Gagal mengubah: {result.get('message', 'Unknown error')}")
    
    except asyncio.TimeoutError:
        await callback_query.message.edit_text("❌ Timeout - tidak ada respon dalam 60 detik.")
    except Exception as e:
        logger.exception("Error editing context")
        await callback_query.message.edit_text(f"❌ Error: {str(e)}")


@app.on_callback_query(filters.regex(r"^admin_forward_edit_value_(\d+)_(\d+)_(.*)$"))
async def on_edit_value(client, callback_query):
    try:
        await callback_query.answer("Edit value...")
    except:
        pass
    
    m = re.match(r"^admin_forward_edit_value_(\d+)_(\d+)_(.*)$", callback_query.data)
    item_id = int(m.group(1))
    page = int(m.group(2))
    search_query = m.group(3) if m.group(3) else None
    
    chat = callback_query.message.chat
    
    try:
        msg = await chat.ask(
            f"🔗 Masukkan **Value** baru untuk item #{item_id}:\n\n"
            f"Contoh:\n"
            f"• `-1001234567890`\n"
            f"• `https://t.me/c/1234567890/5`\n\n"
            f"Kirim /cancel untuk membatalkan.",
            timeout=60
        )
        
        if msg.text and msg.text.startswith("/cancel"):
            return await callback_query.message.edit_text("❌ Dibatalkan.")
        
        new_value = msg.text.strip()
        if not new_value:
            return await callback_query.message.edit_text("❌ Value tidak boleh kosong.")
        
        await callback_query.message.edit_text("⏳ Menyimpan...")
        result = await modify_forward_data("modify", item_id=item_id, value=new_value)
        
        if result.get("success"):
            await callback_query.message.edit_text(f"✅ Value berhasil diubah menjadi: `{new_value}`")
            await asyncio.sleep(2)
            await show_forward_settings(client, callback_query, edit_mode=True, page=page, search_query=search_query)
        else:
            await callback_query.message.edit_text(f"❌ Gagal mengubah: {result.get('message', 'Unknown error')}")
    
    except asyncio.TimeoutError:
        await callback_query.message.edit_text("❌ Timeout - tidak ada respon dalam 60 detik.")
    except Exception as e:
        logger.exception("Error editing value")
        await callback_query.message.edit_text(f"❌ Error: {str(e)}")


@app.on_callback_query(filters.regex(r"^admin_forward_delete_(\d+)_(\d+)_(.*)$"))
async def on_delete_item(client, callback_query):
    m = re.match(r"^admin_forward_delete_(\d+)_(\d+)_(.*)$", callback_query.data)
    item_id = int(m.group(1))
    page = int(m.group(2))
    search_query = m.group(3) if m.group(3) else None
    
    # Get item details for confirmation
    data = await fetch_forward_data()
    item = None
    if data and data.get("success"):
        for i in data.get("data", []):
            if i.get("id") == item_id:
                item = i
                break
    
    if not item:
        return await callback_query.answer("❌ Item tidak ditemukan", show_alert=True)
    
    context = item.get("context") or "(no context)"
    
    # Confirmation
    text = (
        f"⚠️ **Konfirmasi Hapus**\\n\\n"
        f"Yakin ingin menghapus forward option:\\n"
        f"📝 {context}\\n\\n"
        f"Tindakan ini tidak dapat dibatalkan!"
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Hapus", callback_data=f"admin_forward_delete_confirm_{item_id}_{page}_{search_query or ''}")],
        [InlineKeyboardButton("❌ Batal", callback_data=f"admin_forward_settings_refresh_{page}_{search_query or ''}")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^admin_forward_delete_confirm_(\d+)_(\d+)_(.*)$"))
async def on_delete_confirm(client, callback_query):
    try:
        await callback_query.answer("Menghapus...")
    except:
        pass
    
    m = re.match(r"^admin_forward_delete_confirm_(\d+)_(\d+)_(.*)$", callback_query.data)
    item_id = int(m.group(1))
    page = int(m.group(2))
    search_query = m.group(3) if m.group(3) else None
    
    await callback_query.message.edit_text("⏳ Menghapus...")
    result = await modify_forward_data("delete", item_id=item_id)
    
    if result.get("success"):
        await callback_query.message.edit_text(f"✅ Forward option #{item_id} berhasil dihapus!")
        await asyncio.sleep(2)
        await show_forward_settings(client, callback_query, edit_mode=True, page=page, search_query=search_query)
    else:
        await callback_query.message.edit_text(f"❌ Gagal menghapus: {result.get('message', 'Unknown error')}")


async def process_user_media(client, user_id, chat_id):
    """Process all collected media from a user after waiting for all media to arrive"""

    await asyncio.sleep(2)

    all_messages = user_media_collection.pop(user_id, [])
    if not all_messages:
        return

    logger.info(f"Processing {len(all_messages)} total messages from user {user_id}")

    media_groups = defaultdict(list)
    single_media = []

    for msg in all_messages:
        if msg.media_group_id:
            media_groups[msg.media_group_id].append(msg)
            logger.info(f"  Message {msg.id} - media_group_id: {msg.media_group_id}")
        else:
            single_media.append(msg)
            logger.info(f"  Message {msg.id} - single media")

    all_media = []
    for group_id, group_messages in media_groups.items():
        sorted_group = sorted(group_messages, key=lambda m: m.id)
        all_media.extend(sorted_group)
        logger.info(f"  Media group {group_id}: {len(sorted_group)} items")
    all_media.extend(single_media)

    combined_key = f"user_{user_id}_{all_media[0].id}"
    albums[combined_key] = all_media
    logger.info(f"Stored {len(all_media)} media with key: {combined_key}")

    total_count = len(all_media)
    group_count = len(media_groups)
    single_count = len(single_media)

    logger.info(f"Memproses total {total_count} media dari user (groups: {group_count}, single: {single_count})")

    source_msg_id = get_msg_id(all_media[0])

    try:
        if group_count > 0 and single_count > 0:
            status_text = f"{total_count} media diterima ({group_count} grup + {single_count} tunggal). Mengambil daftar tujuan..."
        elif group_count > 1:
            status_text = f"{total_count} media diterima ({group_count} grup media). Mengambil daftar tujuan..."
        elif group_count == 1:
            status_text = f"{total_count} media diterima (1 grup media). Mengambil daftar tujuan..."
        else:
            status_text = f"{total_count} media diterima. Mengambil daftar tujuan..."

        await all_media[0].reply_text(status_text)
    except Exception:
        pass

    # Fetch and cache data
    data = await fetch_forward_data()
    if not data or not data.get("data"):
        return await all_media[0].reply_text("Tidak ada data tujuan yang ditemukan.")

    items = data.get("data") or []
    # Cache for this user
    user_forward_data_cache[user_id] = items
    
    # Store source_msg_id in user state for pagination
    if user_id not in user_search_state:
        user_search_state[user_id] = {}
    user_search_state[user_id]['source_msg_id'] = source_msg_id
    
    # Build paginated keyboard
    markup, total_items, total_pages = build_paginated_keyboard(
        items,
        page=0,
        per_page=10,
        callback_prefix="admin_execute_forward2_",
        search_query=None,
        context_type="forward"
    )
    
    # Replace {source_msg_id} in callback data
    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            if hasattr(btn, 'callback_data') and btn.callback_data:
                new_cb = btn.callback_data.replace("{source_msg_id}", str(source_msg_id))
                new_row.append(InlineKeyboardButton(text=btn.text, callback_data=new_cb))
            else:
                new_row.append(btn)
        new_rows.append(new_row)
    
    markup = InlineKeyboardMarkup(new_rows)
    
    selection_msg = await all_media[0].reply_text("Pilih topik tujuan untuk Forward:", reply_markup=markup)

    albums[f"{combined_key}_selection_msg"] = selection_msg
    
    # Schedule auto-delete with user_id
    await schedule_auto_delete(selection_msg, delay=10, user_id=user_id)

    user_processing_tasks.pop(user_id, None)

@app.on_message(filters.photo | filters.video | filters.document | filters.animation | filters.audio)
async def on_whitelist_media(client, message):

    if not message.from_user or message.from_user.id not in WHITELIST_USER_IDS:
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    user_media_collection[user_id].append(message)

    if user_id in user_processing_tasks:
        user_processing_tasks[user_id].cancel()

    task = asyncio.create_task(process_user_media(client, user_id, chat_id))
    user_processing_tasks[user_id] = task

@app.on_callback_query(filters.regex(r"^admin_execute_forward2_([^_]+)_(\d+)_(\d+)_(\d+)_(\d+)_(.*)$"))
async def on_execute_forward2(client, callback_query):

    logger.info("on_execute_forward2 invoked by user %s: data=%s", callback_query.from_user.id if callback_query.from_user else None, callback_query.data)
    print("DEBUG on_execute_forward2: callback_data=", callback_query.data)

    # Get user_id for cleanup
    user_id = callback_query.from_user.id if callback_query.from_user else None
    
    # Reset auto-delete timer
    await schedule_auto_delete(callback_query.message, delay=60, user_id=user_id)

    # Answer callback query IMMEDIATELY to avoid timeout
    try:
        await callback_query.answer("Memproses forward...")
    except Exception:
        # Ignore if already answered or timeout
        pass

    m = re.match(r"^admin_execute_forward2_([^_]+)_(\d+)_(\d+)_(\d+)_(\d+)_(.*)$", callback_query.data)
    if not m:
        try:
            await callback_query.answer("Format callback tidak valid", show_alert=True)
        except Exception:
            pass
        return

    chat_safe, msg_id_str, reply_msg_id_str, thread_id_str, page_str, search_query = (
        m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
    )

    try:
        target_chat_id = resolve_chat_identifier(chat_safe)
        target_msg_id = int(msg_id_str) if msg_id_str and msg_id_str.isdigit() else None
        source_reply_msg_id = int(reply_msg_id_str)
        thread_id = int(thread_id_str) if thread_id_str and thread_id_str.isdigit() else None
        page = int(page_str) if page_str and page_str.isdigit() else 0
    except ValueError:
        return await callback_query.answer("Data id tidak valid", show_alert=True)

    reply_to = None
    actual_thread_id = thread_id  

    if target_msg_id:
        try:

            tm = await client.get_messages(target_chat_id, target_msg_id)
            if tm and get_msg_id(tm) is not None:
                reply_to = target_msg_id

                if hasattr(tm, 'message_thread_id') and tm.message_thread_id:
                    actual_thread_id = tm.message_thread_id
                    logger.info(f"Got thread_id from target message: {actual_thread_id}")
        except Exception:
            logger.exception("Error fetching target message (auto check)")

            reply_to = None

    logger.info(f"Final parameters: reply_to={reply_to}, actual_thread_id={actual_thread_id}")

    source_chat_id = callback_query.message.chat.id

    logger.info(f"Looking for album with source_reply_msg_id: {source_reply_msg_id}")
    logger.info(f"Available albums keys: {list(albums.keys())}")

    media_group_id = None
    album = []
    for group_id, messages in albums.items():
        # Skip if it's selection_msg (single Message object)
        if group_id.endswith('_selection_msg'):
            continue
        # Check if messages is a list and has the matching source_reply_msg_id
        if isinstance(messages, list) and messages and get_msg_id(messages[0]) == source_reply_msg_id:
            media_group_id = group_id
            album = albums.pop(group_id)
            logger.info(f"Found album with key: {group_id}, contains {len(album)} messages")
            break

    if not album:
        logger.warning(f"No album found for message ID: {source_reply_msg_id}")

    if album:

        logger.info(f"Memproses media group dengan {len(album)} item")

        selection_msg = albums.pop(f"{media_group_id}_selection_msg", None)

        try:

            media_list = []
            for idx, msg in enumerate(album):
                caption = msg.caption if msg.caption and idx == 0 else None
                if msg.photo:
                    media_list.append(InputMediaPhoto(msg.photo.file_id, caption=caption))
                elif msg.video:
                    media_list.append(InputMediaVideo(msg.video.file_id, caption=caption))

            if not media_list:
                try:
                    await callback_query.answer("Tidak ada media yang valid dalam group.", show_alert=True)
                except Exception:
                    pass
                return

            batch_size = 10
            total_batches = (len(media_list) + batch_size - 1) // batch_size  

            logger.info(f"Mengirim {len(media_list)} media dalam {total_batches} batch(es) ke chat: {target_chat_id}, thread: {actual_thread_id}, reply_to: {reply_to}")

            if total_batches > 1 and selection_msg:
                try:
                    await selection_msg.edit_text(f"📤 Mengirim {len(media_list)} media dalam {total_batches} grup...\n\n⏳ Progress: 0/{total_batches} grup terkirim")
                except Exception:
                    pass

            sent_count = 0
            for i in range(0, len(media_list), batch_size):
                batch = media_list[i:i + batch_size]
                batch_num = (i // batch_size) + 1

                kwargs = {
                    'chat_id': target_chat_id,
                    'media': batch,
                }

                # Note: send_media_group di Pyrogram 2.3.69 tidak mendukung message_thread_id
                # Media group akan dikirim ke chat utama, bukan ke thread tertentu
                
                if reply_to:
                    kwargs['reply_to_message_id'] = reply_to

                logger.info(f"Mengirim batch {batch_num}/{total_batches} dengan {len(batch)} item - kwargs: chat_id={target_chat_id}, thread_id={actual_thread_id}, reply_to={reply_to}")

                try:
                    sent = await client.send_media_group(**kwargs)
                    sent_count += len(batch)
                    logger.info(f"Batch {batch_num}/{total_batches} berhasil dikirim: {len(batch)} item")
                except TypeError as te:

                    if "missing 1 required keyword-only argument: 'topics'" in str(te):
                        sent_count += len(batch)
                        logger.warning(f"Batch {batch_num}/{total_batches} terkirim tetapi terjadi bug parsing response Pyrogram (diabaikan)")
                    else:
                        raise

                if total_batches > 1 and selection_msg:
                    try:
                        progress_bar = "▓" * batch_num + "░" * (total_batches - batch_num)
                        await selection_msg.edit_text(
                            f"📤 Mengirim {len(media_list)} media dalam {total_batches} grup...\n\n"
                            f"[{progress_bar}] {batch_num}/{total_batches}\n"
                            f"✅ {sent_count}/{len(media_list)} media terkirim"
                        )
                    except Exception:
                        pass

                if i + batch_size < len(media_list):
                    await asyncio.sleep(0.5)

            logger.info(f"Total {sent_count} media berhasil dikirim dalam {total_batches} batch(es)")
        except Exception as e:
            logger.exception("Gagal mengirim media group")
            try:
                await callback_query.answer("Gagal melakukan forward media group.", show_alert=True)
            except Exception:
                pass
            return

        if selection_msg:
            try:
                if reply_to:
                    await selection_msg.edit_text(
                        f"✅ Forward berhasil!\n\n"
                        f"📊 Total: {len(media_list)} media\n"
                        f"📦 Dikirim dalam: {total_batches} grup\n"
                        f"💬 Reply ke: Message #{reply_to}"
                    )
                else:
                    await selection_msg.edit_text(
                        f"✅ Forward berhasil!\n\n"
                        f"📊 Total: {len(media_list)} media\n"
                        f"📦 Dikirim dalam: {total_batches} grup"
                    )
            except Exception:
                pass
        
        # Cleanup user state after successful forward
        if user_id:
            user_forward_data_cache.pop(user_id, None)
            user_search_state.pop(user_id, None)

        # Don't answer callback query here - already answered at the start
    else:

        try:
            source_message = await client.get_messages(source_chat_id, source_reply_msg_id)
        except Exception:
            logger.exception("Gagal mengambil pesan sumber sebelum auto forward")
            try:
                await callback_query.answer("Gagal mengambil pesan sumber.", show_alert=True)
            except Exception:
                pass
            return

        if not source_message or get_msg_id(source_message) is None:
            try:
                await callback_query.answer("Pesan sumber tidak ditemukan.", show_alert=True)
            except Exception:
                pass
            return

        filtered_markup = filter_url_markup(source_message.reply_markup)
        caption_override = "" if getattr(source_message, "caption", None) else None

        try:

            kwargs = {
                'chat_id': target_chat_id,
                'from_chat_id': source_chat_id,
                'message_id': source_reply_msg_id,
                'reply_markup': filtered_markup,
                'caption': caption_override,
            }
            if thread_id:
                kwargs['message_thread_id'] = thread_id
            if reply_to:
                kwargs['reply_to_message_id'] = reply_to

            sent = await client.copy_message(**kwargs)
            logger.info("copy_message result (auto): %s", sent)
            print("DEBUG sent message object (auto):", sent)
            logger.info("sent.reply_to_message: %s", getattr(sent, 'reply_to_message', None))
        except Exception:
            logger.exception("Gagal menyalin dan mem-forward pesan secara auto")
            try:
                return await callback_query.answer("Gagal melakukan forward. Pastikan saya memiliki akses ke grup dan topik.", show_alert=True)
            except Exception:
                # Callback might have timed out
                pass
            return

        # Update message text only (callback already answered at start)
        try:
            if reply_to:
                await callback_query.message.edit_text("Forward ke topic berhasil (reply) ✅")
            else:
                await callback_query.message.edit_text("Forward ke topic berhasil (sebagai pesan baru) ✅")
        except Exception:
            pass
        
        # Cleanup user state after successful forward
        if user_id:
            user_forward_data_cache.pop(user_id, None)
            user_search_state.pop(user_id, None)

@app.on_callback_query(filters.regex(r"^admin_extend_forward1_(\d+)_(\d+)_(\d+)$"))
async def on_extend_forward1(client, callback_query):

    m = re.match(r"^admin_extend_forward1_(\d+)_(\d+)_(\d+)$", callback_query.data)
    if not m:
        return await callback_query.answer("Format callback tidak valid", show_alert=True)

    chatid_no_minus, msg_id_str, reply_msg_id_str = m.group(1), m.group(2), m.group(3)

    try:

        target_chat_id = reconstruct_chat_id(chatid_no_minus)
        target_msg_id = int(msg_id_str)
        source_reply_msg_id = int(reply_msg_id_str)
    except ValueError:
        return await callback_query.answer("Data id tidak valid", show_alert=True)

    try:
        target_message = await client.get_messages(target_chat_id, target_msg_id)
    except Exception as e:
        logger.exception("Error fetching target message")
        return await callback_query.answer("Gagal memeriksa pesan tujuan.", show_alert=True)

    if not target_message or get_msg_id(target_message) is None:
        return await callback_query.answer("Pesan tujuan tidak ditemukan.", show_alert=True)

    source_chat_id = callback_query.message.chat.id
    try:
        source_message = await client.get_messages(source_chat_id, source_reply_msg_id)
    except Exception:
        logger.exception("Gagal mengambil pesan sumber")
        return await callback_query.answer("Gagal mengambil pesan sumber.", show_alert=True)

    if not source_message or get_msg_id(source_message) is None:
        return await callback_query.answer("Pesan sumber tidak ditemukan.", show_alert=True)

    filtered_markup = filter_url_markup(source_message.reply_markup)

    caption_override = "" if getattr(source_message, "caption", None) else None

    try:
        await client.copy_message(
            chat_id=target_chat_id,
            from_chat_id=source_chat_id,
            message_id=source_reply_msg_id,
            reply_to_message_id=target_msg_id,
            reply_markup=filtered_markup,
            caption=caption_override,
        )
    except Exception:
        logger.exception("Gagal menyalin dan mem-forward pesan")
        return await callback_query.answer("Gagal melakukan forward. Pastikan saya memiliki akses ke chat tujuan.", show_alert=True)

    await callback_query.answer("Forward berhasil ✅")
    await callback_query.message.edit_text("Forward berhasil ✅")

@app.on_callback_query(filters.regex(r"^admin_extend_forward2_([^_]+)_(\d+)_(\d+)_(\d+)$"))
async def on_extend_forward2(client, callback_query):

    logger.info("on_extend_forward2 invoked by user %s: data=%s", callback_query.from_user.id if callback_query.from_user else None, callback_query.data)
    print("DEBUG on_extend_forward2: callback_data=", callback_query.data)

    await callback_query.answer("Memproses forward ke topic...")

    try:
        m = re.match(r"^admin_extend_forward2_(\d+)_(\d+)_(\d+)_(\d+)$", callback_query.data)
        if not m:
            return await callback_query.answer("Format callback tidak valid", show_alert=True)

        chatid_no_minus, msg_id_str, reply_msg_id_str, thread_id_str = m.group(1), m.group(2), m.group(3), m.group(4)

        try:
            target_chat_id = reconstruct_chat_id(chatid_no_minus)
            target_msg_id = int(msg_id_str)
            source_reply_msg_id = int(reply_msg_id_str)
            thread_id = int(thread_id_str)
        except ValueError:
            return await callback_query.answer("Data id tidak valid", show_alert=True)

        try:
            target_message = await client.get_messages(target_chat_id, target_msg_id)
        except Exception:
            logger.exception("Error fetching target message for forward2")
            return await callback_query.answer("Gagal memeriksa pesan tujuan.", show_alert=True)

        if not target_message or get_msg_id(target_message) is None:
            return await callback_query.answer("Pesan tujuan tidak ditemukan.", show_alert=True)

        source_chat_id = callback_query.message.chat.id
        try:
            source_message = await client.get_messages(source_chat_id, source_reply_msg_id)
        except Exception:
            logger.exception("Gagal mengambil pesan sumber")
            return await callback_query.answer("Gagal mengambil pesan sumber.", show_alert=True)

        if not source_message or get_msg_id(source_message) is None:
            return await callback_query.answer("Pesan sumber tidak ditemukan.", show_alert=True)

        filtered_markup = filter_url_markup(source_message.reply_markup)
        caption_override = "" if getattr(source_message, "caption", None) else None

        try:

            await client.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_reply_msg_id,
                message_thread_id=thread_id,
                reply_markup=filtered_markup,
                caption=caption_override,
            )
        except Exception:
            logger.exception("Gagal menyalin dan mem-forward pesan ke thread sebagai pesan baru")
            return await callback_query.answer("Gagal melakukan forward ke thread. Pastikan saya memiliki akses ke grup dan topik.", show_alert=True)

        await callback_query.answer("Forward ke topic berhasil (sebagai pesan baru) ✅")
        await callback_query.message.edit_text("Forward ke topic berhasil (sebagai pesan baru) ✅")
    except Exception as e:
        logger.exception("Unhandled error in on_extend_forward2")
        await callback_query.answer("Terjadi kesalahan internal saat memproses forward.", show_alert=True)

@app.on_callback_query(filters.regex(r"^admin_cancel$"))
async def on_cancel(client, callback_query):
    await callback_query.answer("Dibatalkan")
    
    # Get user_id for cleanup
    user_id = callback_query.from_user.id if callback_query.from_user else None
    
    try:
        # Delete the message instead of editing
        await callback_query.message.delete()
        # Also cancel any pending auto-delete tasks
        message_key = f"{callback_query.message.chat.id}_{callback_query.message.id}"
        if message_key in button_messages:
            button_messages[message_key]['task'].cancel()
            button_messages.pop(message_key, None)
        
        # Cleanup user states
        if user_id:
            user_search_state.pop(user_id, None)
            user_forward_data_cache.pop(user_id, None)
    except Exception as e:
        logger.exception(f"Error deleting cancelled message: {e}")
        # Fallback to editing if delete fails
        try:
            await callback_query.message.edit_text("❌ Dibatalkan oleh pengguna.")
        except:
            pass

@flask_app.route('/healthz', methods=['GET'])
def health_check():
    return "OK", 200

def run_flask():
    global flask_thread_running
    flask_thread_running = True
    port = int(os.environ.get('PORT', 3251)) 
    flask_app.run(host="0.0.0.0", port=port)
    while flask_thread_running:
        time.sleep(3) 
    logger.info("Flask thread stopped gracefully.")

async def run_pyrogram_async():
    try:
        await app.start()
        logger.info("Bot berjalan...")
        bot_info = await app.get_me()
        logger.info(f"""
        =================
        Bot Information:
        Username: @{bot_info.username}
        Name: {bot_info.first_name}
        Bot ID: {bot_info.id}
        =================
        """)
        logger.info("Bot has started successfully!")
        
        await idle()
        
    except Exception as e:
        logger.error(f"Error occurred while running Pyrogram: {e}")
    finally:
        await app.stop()
        logger.info("Bot telah berhenti.")


if __name__ == '__main__':
    try:

        logger.info("Starting Flask server...")
        flask_thread = Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()
        
        loop = asyncio.get_event_loop()

        async_tasks = [run_pyrogram_async()]

        loop.run_until_complete(
            asyncio.gather(*async_tasks)
        )
        
    except KeyboardInterrupt:
        logger.info("Menghentikan bot...")
    finally:
        logger.info("Bot telah dihentikan.")


