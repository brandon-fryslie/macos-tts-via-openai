import os
import subprocess
import sys
import logging
import pyaudio
import requests
import openai
import io
from queue import Queue
import threading
from colorama import Fore, Style

# Configure logging with enhanced color formatting
class EnhancedColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.BLUE + Style.BRIGHT,
        logging.INFO: Fore.GREEN + Style.BRIGHT,
        logging.WARNING: Fore.YELLOW + Style.BRIGHT + Style.BRIGHT,
        logging.ERROR: Fore.RED + Style.BRIGHT + Style.BRIGHT,
        logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT + Style.BRIGHT,
    }

    def format(self, record):
        level_color = self.LEVEL_COLORS.get(record.levelno, "")
        reset = Style.RESET_ALL
        record.levelname = f"{level_color}{record.levelname.upper()}{reset}"
        record.msg = f"{level_color}{record.msg}{reset}"
        return super().format(record)

# Setup logging with both console and file handlers
log_file = "/tmp/openai-tts.log"

# File handler
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(EnhancedColorFormatter("%(asctime)s - %(levelname)s - %(message)s"))

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(EnhancedColorFormatter("%(asctime)s - %(levelname)s - %(message)s"))

# Configure logging
logging.basicConfig(level=logging.DEBUG, handlers=[console_handler, file_handler])

# Retrieve OpenAI API key
def get_openai_key():
    try:
        logging.debug("Retrieving OpenAI API key from Keychain...")
        key = subprocess.check_output(
            ["security", "find-generic-password", "-a", "bmf", "-s", "OpenAI_API_Key", "-w"],
            universal_newlines=True
        ).strip()
        logging.debug("Successfully retrieved OpenAI API key.")
        return key
    except subprocess.CalledProcessError as e:
        logging.error("Failed to retrieve OpenAI API key from Keychain.")
        raise Exception("Failed to retrieve OpenAI API key from Keychain") from e

# Stream audio from OpenAI
def stream_audio_from_openai(api_url, params, headers, audio_queue):
    logging.debug("Starting audio stream from OpenAI...")
    try:
        response = requests.post(api_url, json=params, headers=headers, stream=True)
        response.raise_for_status()
        logging.debug("Audio stream connected successfully.")
        for chunk in response.iter_content(chunk_size=16384):
            if chunk:
                logging.debug(f"Received audio chunk of size {len(chunk)} bytes.")
                audio_queue.put(chunk)
    except Exception as e:
        logging.error(f"Failed to stream audio: {e}")
    finally:
        logging.debug("Streaming ended. Sending sentinel to audio queue.")
        audio_queue.put(None)

# Decode and play audio from the queue
def play_audio_from_queue(audio_queue, initial_buffer_size=262144, chunk_size=16384):
    logging.debug("Initializing PyAudio for playback...")
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16, channels=1, rate=24000, output=True)
    audio_buffer = io.BytesIO()
    buffered_size = 0

    def decode_and_stream(buffer):
        """Decode MP3 data using a separate ffmpeg process and stream it to PyAudio."""
        try:
            logging.debug(f"Buffer size before decoding: {len(buffer.getvalue())} bytes")
            process = subprocess.Popen(
                ["/opt/homebrew/Cellar/ffmpeg/7.1/bin/ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ar", "24000", "-ac", "1", "pipe:1"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10**8,
            )
            pcm_data, stderr_output = process.communicate(buffer.getvalue())
            if stderr_output:
                logging.debug(f"ffmpeg stderr: {stderr_output.decode()}")
            if process.returncode != 0:
                logging.error(f"ffmpeg exited with code {process.returncode}")
                return
            logging.debug(f"Writing {len(pcm_data)} bytes of PCM data to PyAudio stream.")
            stream.write(pcm_data)
        except Exception as e:
            logging.error(f"Error decoding with ffmpeg: {e}")

    try:
        while True:
            chunk = audio_queue.get()
            if chunk is None:
                logging.debug("End of audio stream detected. Processing remaining buffer...")
                break
            audio_buffer.write(chunk)
            buffered_size += len(chunk)
            logging.debug(f"Buffered {buffered_size} bytes.")
            if buffered_size >= initial_buffer_size:
                logging.debug("Decoding audio with ffmpeg...")
                decode_and_stream(audio_buffer)
                audio_buffer = io.BytesIO()
                buffered_size = 0
        if buffered_size > 0:
            logging.debug("Processing remaining audio in buffer...")
            decode_and_stream(audio_buffer)
    except Exception as e:
        logging.error(f"Error during audio playback: {e}")
    finally:
        logging.debug("Stopping PyAudio stream...")
        stream.stop_stream()
        stream.close()
        p.terminate()
        logging.debug("Playback complete.")

# Generate and play speech
def generate_and_play_speech(text):
    logging.debug("Preparing to generate and play speech...")
    api_url = "https://api.openai.com/v1/audio/speech"
    params = {
        "model": "tts-1-hd",
        "voice": "echo",
        "input": text,
        "rate": 0.85
    }
    headers = {
        "Authorization": f"Bearer {get_openai_key()}",
        "Content-Type": "application/json"
    }
    audio_queue = Queue()
    stream_thread = threading.Thread(target=stream_audio_from_openai, args=(api_url, params, headers, audio_queue))
    play_thread = threading.Thread(target=play_audio_from_queue, args=(audio_queue, 65536))
    stream_thread.start()
    play_thread.start()
    stream_thread.join()
    audio_queue.put(None)
    play_thread.join()
    logging.debug("Speech generation and playback complete.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        logging.error("No text provided. Usage: python script.py <text>")
        sys.exit(1)
    text = " ".join(sys.argv[1:])
    logging.info(f"Input text: {text}")
    try:
        generate_and_play_speech(text)
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        sys.exit(1)
