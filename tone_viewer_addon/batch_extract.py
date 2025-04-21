# batch_extract.py
# Standalone script to bulk process MP3s in Anki's media folder.

import os
import sys
import argparse
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Assume this script is run from the addon directory or add vendor path
# Add vendor directory to sys.path if running from outside addon dir
addon_dir = os.path.dirname(__file__)
vendor_dir = os.path.join(addon_dir, "vendor")
if vendor_dir not in sys.path:
    sys.path.insert(0, vendor_dir)

# Now import the processor
try:
    from mp3_processor import process_mp3_to_json
except ImportError as e:
    print(f"Error: Could not import mp3_processor. Make sure libraries are vendored correctly.", file=sys.stderr)
    print(f"Details: {e}", file=sys.stderr)
    sys.exit(1)

def find_anki_media_folder() -> str | None:
    """Tries to find the Anki media folder on macOS."""
    # Default Anki2 data folder location on macOS
    anki_data_path = Path.home() / "Library/Application Support/Anki2"
    # Find the profile name (usually the first non-addon folder)
    profile_name = None
    if anki_data_path.is_dir():
        for item in anki_data_path.iterdir():
            if item.is_dir() and not item.name.startswith("addons") and item.name != "global":
                profile_name = item.name
                break
    
    if profile_name:
        media_path = anki_data_path / profile_name / "collection.media"
        if media_path.is_dir():
            return str(media_path)
            
    print("Warning: Could not automatically find Anki collection.media folder.", file=sys.stderr)
    print("Please specify it using the --media-folder argument.", file=sys.stderr)
    return None

def run_batch_processing(media_folder: str, max_workers: int | None = None, force_overwrite: bool = False):
    """Finds MP3s and processes them using multiple cores."""
    print(f"Scanning for MP3 files in: {media_folder}")
    mp3_files = list(Path(media_folder).glob('*.mp3'))
    print(f"Found {len(mp3_files)} MP3 files.")

    if not mp3_files:
        print("No MP3 files found to process.")
        return

    tasks = []
    skipped_count = 0
    processed_count = 0
    error_count = 0
    start_time = time.time()

    print(f"Preparing tasks (Max Workers: {max_workers or 'Default'})... Force Overwrite: {force_overwrite}")
    for mp3_path_obj in mp3_files:
        mp3_path = str(mp3_path_obj)
        json_path = str(mp3_path_obj.with_suffix('.json'))

        # Check if JSON exists and if we should overwrite
        if not force_overwrite and os.path.exists(json_path):
            try:
                # Optional: Check if JSON is newer than MP3
                mp3_mtime = os.path.getmtime(mp3_path)
                json_mtime = os.path.getmtime(json_path)
                if json_mtime >= mp3_mtime:
                    # print(f"Skipping {os.path.basename(mp3_path)}: JSON already exists and is up-to-date.")
                    skipped_count += 1
                    continue # Skip this file
                else:
                    print(f"Reprocessing {os.path.basename(mp3_path)}: JSON is older than MP3.")
            except OSError:
                # Handle potential race condition or file not found error
                pass # Proceed to process
        
        tasks.append((mp3_path, json_path))

    total_tasks = len(tasks)
    print(f"Processing {total_tasks} files (Skipped {skipped_count} up-to-date files)...")

    if not tasks:
        print("No files need processing.")
        return

    # Use ProcessPoolExecutor for parallel processing
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        # Submit tasks
        futures = {executor.submit(process_mp3_to_json, mp3, json_f): mp3 for mp3, json_f in tasks}
        
        # Process results as they complete
        for i, future in enumerate(as_completed(futures)):
            mp3_file = futures[future]
            basename = os.path.basename(mp3_file)
            progress = f"[{(i+1):>4}/{total_tasks}]"
            try:
                success = future.result() # Get result (True/False)
                if success:
                    print(f"{progress} Successfully processed: {basename}")
                    processed_count += 1
                else:
                    print(f"{progress} Failed (no data/error): {basename}")
                    error_count += 1
            except Exception as exc:
                print(f"{progress} Exception processing {basename}: {exc}", file=sys.stderr)
                error_count += 1

    end_time = time.time()
    duration = end_time - start_time
    print("\n--- Batch Processing Summary ---")
    print(f"Total time: {duration:.2f} seconds")
    print(f"Successfully processed: {processed_count}")
    print(f"Skipped (up-to-date): {skipped_count}")
    print(f"Failed or no data:    {error_count}")
    print("------------------------------")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk extract pitch contours from MP3s in Anki media folder.")
    parser.add_argument(
        "--media-folder", 
        help="Path to Anki's collection.media folder. Tries to auto-detect if not provided."
    )
    parser.add_argument(
        "-w", "--max-workers", 
        type=int, 
        default=None, 
        help="Maximum number of worker processes to use (default: system default)."
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force reprocessing of files even if JSON exists."
    )

    args = parser.parse_args()

    media_folder = args.media_folder
    if not media_folder:
        media_folder = find_anki_media_folder()
    
    if not media_folder or not os.path.isdir(media_folder):
        print(f"Error: Media folder not found or invalid: {media_folder}", file=sys.stderr)
        sys.exit(1)
        
    run_batch_processing(media_folder, args.max_workers, args.force) 