# mp3_processor.py
# Handles offline pitch extraction from MP3 files.

import os
import sys
import json
import numpy as np

# Need to ensure aubio and soundfile are available
try:
    # Assume they are vendored alongside this file
    vendor_path = os.path.join(os.path.dirname(__file__), "vendor")
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)
    import aubio
    import soundfile as sf
except ImportError as e:
    print(f"MP3 Processor Error: Failed to import aubio or soundfile from vendor. {e}", file=sys.stderr)
    # Handle the error appropriately, maybe raise it or set flags
    raise

# --- Constants (can be adjusted) ---
# Use the same parameters as real-time for consistency?
DEFAULT_PITCH_METHOD = "yin"
DEFAULT_BUF_SIZE = 2048
# WARNING: Very small hop size leads to significantly longer processing time for MP3s!
DEFAULT_HOP_SIZE = 512    # Increased hop size back to 64 for better performance
DEFAULT_SILENCE_DB = -40
# Confidence threshold for offline processing - can maybe be slightly higher?
DEFAULT_CONF_THRESHOLD = 0.5 # Standardized to match real-time
DEFAULT_CLAMP_LOW_HZ = 50
DEFAULT_CLAMP_HIGH_HZ = 200 # Keep user's range

def extract_pitch_data(audio_signal: np.ndarray, sample_rate: int) -> list[list[float]]:
    """Extracts pitch from an audio signal array."""
    pitch_o = aubio.pitch(
        DEFAULT_PITCH_METHOD,
        DEFAULT_BUF_SIZE,
        DEFAULT_HOP_SIZE,
        sample_rate
    )
    pitch_o.set_unit("Hz")
    pitch_o.set_silence(DEFAULT_SILENCE_DB)
    # No tolerance setting for plain yin

    total_frames = len(audio_signal)
    pitch_data = []
    current_frame = 0

    while current_frame + DEFAULT_HOP_SIZE < total_frames:
        frame = audio_signal[current_frame : current_frame + DEFAULT_HOP_SIZE]
        pitch = pitch_o(frame)[0]
        confidence = pitch_o.get_confidence()

        if confidence >= DEFAULT_CONF_THRESHOLD:
            # Clamp pitch
            if DEFAULT_CLAMP_LOW_HZ <= pitch <= DEFAULT_CLAMP_HIGH_HZ:
                time_ms = (current_frame + DEFAULT_HOP_SIZE / 2) / sample_rate * 1000
                pitch_data.append([time_ms, float(pitch)])
            # else: Clamped out, treat as unvoiced
        # else: Low confidence, treat as unvoiced

        current_frame += DEFAULT_HOP_SIZE

    return pitch_data

def process_mp3_to_json(mp3_path: str, output_json_path: str) -> bool:
    """Loads an MP3, extracts pitch, and saves to JSON."""
    try:
        print(f"Processing: {os.path.basename(mp3_path)} -> {os.path.basename(output_json_path)}")
        # 1. Load MP3 using soundfile
        # Ensure signal is mono float32
        signal, samplerate = sf.read(mp3_path, dtype='float32', always_2d=False)
        print(f"  Loaded MP3: Sample Rate={samplerate}, Duration={len(signal)/samplerate:.2f}s")

        # Ensure mono if stereo (take average or first channel)
        if signal.ndim > 1:
            print("  Audio is stereo, converting to mono.")
            signal = np.mean(signal, axis=1) # Average channels
            # Or signal = signal[:, 0] # Take first channel

        # 2. Extract pitch data
        pitch_points = extract_pitch_data(signal, samplerate)
        print(f"  Extracted {len(pitch_points)} valid pitch points.")

        if not pitch_points:
            print("  Warning: No valid pitch points extracted. Skipping JSON output.")
            return False # Indicate no useful data was extracted

        # 3. Save to JSON
        with open(output_json_path, 'w') as f:
            json.dump(pitch_points, f)
        print(f"  Saved JSON successfully.")
        return True

    except sf.SoundFileError as sfe:
        print(f"  Error loading audio file {mp3_path}: {sfe}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  Error processing file {mp3_path}: {e}", file=sys.stderr)
        # Consider deleting potentially incomplete JSON file if error occurred during write?
        # if os.path.exists(output_json_path): os.remove(output_json_path)
        return False

# Example usage (for testing)
if __name__ == '__main__':
    # This block runs only when the script is executed directly
    # Replace with actual paths for testing
    test_mp3 = 'path/to/your/test.mp3' 
    test_json = 'path/to/your/test.json' 
    if os.path.exists(test_mp3):
        process_mp3_to_json(test_mp3, test_json)
    else:
        print(f"Test file not found: {test_mp3}") 