"""
Vitodata Dictation Desktop Client
===================================
Runs on your Windows PC. Connects to the streaming dictation server
on p2aisv01 and types text wherever your cursor is — Word, Notepad,
any editor, any app.

Install:
  pip install python-socketio[client] pyperclip keyboard pystray Pillow

Run:
  python dictation_client.py

Usage:
  1. Script starts and connects to the server
  2. Press F8 to start dictation (mic starts recording)
  3. Speak — text appears wherever your cursor is
  4. Press F8 again to stop
  5. Press F9 to quit

The text is inserted via clipboard (Ctrl+V) so it handles
German umlauts, special chars, and any Unicode perfectly.
"""

import sys
import time
import threading

import numpy as np
import pyaudio
import pyperclip
import socketio
import keyboard  # for global hotkeys and simulating Ctrl+V

# ---------------------------------------------------------------------------
# Configuration — change this to your server
# ---------------------------------------------------------------------------
SERVER_URL = "http://172.17.16.150:8003"

HOTKEY_TOGGLE = "F8"    # press to start/stop dictation
HOTKEY_QUIT = "F9"      # press to quit the app

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
sio = socketio.Client(reconnection=True)
is_dictating = False
confirmed_buffer = ""   # tracks what we've already typed

# Audio recording settings (must match server expectations)
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 4096  # samples per chunk (~256ms at 16kHz)
FORMAT = pyaudio.paInt16

audio_stream = None
audio_interface = None
recording_thread = None
stop_recording = threading.Event()


# ---------------------------------------------------------------------------
# Audio Recording + Streaming
# ---------------------------------------------------------------------------

def record_and_stream():
    """Record from microphone and send PCM chunks to the server."""
    global audio_stream, audio_interface

    audio_interface = pyaudio.PyAudio()
    audio_stream = audio_interface.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    print("  [mic] Recording started")
    stop_recording.clear()

    while not stop_recording.is_set():
        try:
            data = audio_stream.read(CHUNK_SIZE, exception_on_overflow=False)
            if sio.connected:
                sio.emit("audio_chunk", data)
        except Exception as e:
            print(f"  [mic] Error: {e}")
            break

    audio_stream.stop_stream()
    audio_stream.close()
    audio_interface.terminate()
    print("  [mic] Recording stopped")


def start_recording():
    global recording_thread
    stop_recording.clear()
    recording_thread = threading.Thread(target=record_and_stream, daemon=True)
    recording_thread.start()


def stop_recording_func():
    stop_recording.set()
    if recording_thread:
        recording_thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Type text at cursor position (any app)
# ---------------------------------------------------------------------------

def type_at_cursor(text):
    """
    Insert text wherever the cursor is, in any application.
    Uses clipboard + Ctrl+V for full Unicode support (umlauts etc).
    """
    if not text:
        return

    # Save current clipboard
    try:
        old_clipboard = pyperclip.paste()
    except Exception:
        old_clipboard = ""

    # Put our text in clipboard and paste
    pyperclip.copy(text)
    time.sleep(0.05)  # small delay for clipboard to settle
    keyboard.send("ctrl+v")
    time.sleep(0.05)

    # Restore old clipboard (optional — comment out if it causes issues)
    # pyperclip.copy(old_clipboard)


# ---------------------------------------------------------------------------
# Socket.IO Events
# ---------------------------------------------------------------------------

@sio.event
def connect():
    print(f"  [ws] Connected to {SERVER_URL}")


@sio.event
def disconnect():
    print("  [ws] Disconnected")


@sio.event
def dictation_started():
    print("  [ws] Dictation session started on server")


@sio.on("transcription")
def on_transcription(data):
    """
    Receive transcription from server.
    Only type NEW confirmed text (avoid re-typing what's already been typed).
    """
    global confirmed_buffer

    confirmed = data.get("confirmed", "")
    provisional = data.get("provisional", "")
    is_final = data.get("is_final", False)

    if is_final:
        # Final pass (with LLM correction) — type the full corrected text
        # But first we need to figure out what's new vs already typed
        # Simple approach: on final, the LLM-corrected text replaces everything
        # For now, just type any new text beyond what we've buffered
        full_text = confirmed
        if len(full_text) > len(confirmed_buffer):
            new_text = full_text[len(confirmed_buffer):]
            if new_text.strip():
                type_at_cursor(new_text)
                print(f"  [final] Typed: '{new_text.strip()}'")
        confirmed_buffer = full_text
    else:
        # Streaming update — only type newly confirmed text
        if confirmed and len(confirmed) > len(confirmed_buffer):
            new_text = confirmed[len(confirmed_buffer):]
            if new_text.strip():
                # Add a space before if we're continuing
                if confirmed_buffer and not new_text.startswith(" "):
                    new_text = " " + new_text
                type_at_cursor(new_text)
                print(f"  [live]  Typed: '{new_text.strip()}'")
            confirmed_buffer = confirmed

        # Show provisional in console (not typed — it might change)
        if provisional:
            print(f"  [provisional] {provisional}", end="\r")


# ---------------------------------------------------------------------------
# Hotkey Handlers
# ---------------------------------------------------------------------------

def toggle_dictation():
    global is_dictating, confirmed_buffer

    if not sio.connected:
        print("  [!] Not connected to server")
        return

    if is_dictating:
        # Stop
        stop_recording_func()
        sio.emit("stop_dictation")
        is_dictating = False
        confirmed_buffer = ""
        print("\n  === DICTATION STOPPED (press F8 to start) ===")
    else:
        # Start
        confirmed_buffer = ""
        sio.emit("start_dictation", {"field": "general"})
        start_recording()
        is_dictating = True
        print("\n  === DICTATION STARTED — speak now (press F8 to stop) ===")
        print("  Text will appear wherever your cursor is.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 56)
    print("  Vitodata Dictation Desktop Client")
    print("=" * 56)
    print(f"  Server:  {SERVER_URL}")
    print(f"  Toggle:  {HOTKEY_TOGGLE} (start/stop dictation)")
    print(f"  Quit:    {HOTKEY_QUIT}")
    print("=" * 56)
    print()

    # Connect to server
    print("  Connecting to server...")
    try:
        sio.connect(SERVER_URL, transports=["websocket"])
    except Exception as e:
        print(f"  [!] Could not connect: {e}")
        print(f"  [!] Make sure the server is running at {SERVER_URL}")
        sys.exit(1)

    # Register global hotkeys
    keyboard.add_hotkey(HOTKEY_TOGGLE, toggle_dictation, suppress=True)
    print(f"  Press {HOTKEY_TOGGLE} to start dictating...")
    print(f"  Press {HOTKEY_QUIT} to quit.\n")

    # Wait for quit
    keyboard.wait(HOTKEY_QUIT)

    # Cleanup
    print("\n  Shutting down...")
    if is_dictating:
        stop_recording_func()
        sio.emit("stop_dictation")
    sio.disconnect()
    print("  Bye!")


if __name__ == "__main__":
    main()