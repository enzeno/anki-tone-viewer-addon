# Tone Viewer Anki Addon

## Overview

The Tone Viewer addon provides visual feedback on pitch contours for language learners, particularly those studying tonal languages like Mandarin Chinese. It allows users to compare their own spoken pitch against a target audio sample directly within the Anki reviewer interface.

## Features

*   **Pitch Contour Visualization:** Displays a graph plotting pitch (in cents relative to a baseline) over time.
*   **Target Pitch Display:** Loads pitch data extracted from audio files (.mp3) stored in specific note fields (HanziAudio, Example1Audio, Example2Audio, Example3Audio) and displays it as an orange target contour on the graph.
*   **Live Pitch Recording:** Allows users to record their own voice using the microphone.
*   **Real-time Feedback:** Plots the user's live pitch contour (blue line) alongside the target contour for immediate comparison.
*   **Multiple Target Selection:** If multiple audio fields have corresponding pitch data, users can switch between targets (1-4) using dedicated buttons.
*   **Dynamic Time Axis:** The graph's time scale automatically adjusts to match the duration of the selected target audio.
*   **Playback Speed Simulation:** Includes buttons (1x, 0.75x, 0.5x, 0.25x) to adjust the *time scaling* of the live recording plot, simulating slower playback for easier comparison (note: this does *not* slow down the actual audio playback).
*   **Bulk Audio Processing:** Provides a tool (Tools > Tone Viewer > Bulk Process Audio...) to analyze MP3 files stored in notes and pre-generate the required pitch data (.json files) for faster loading during review.
*   **Parallel Processing:** The bulk processing utilizes multiple CPU cores for significant speed improvements on large collections.

## How it Works

1.  **Preprocessing (Bulk or Single):** The addon uses the Aubio library to analyze `.mp3` audio files found in specified note fields. It extracts fundamental frequency (F0) estimates over time.
2.  **JSON Data:** This pitch data (timestamps and F0 in Hz) is saved as a `.json` file alongside the original `.mp3` file in the Anki media collection (e.g., `[sound:my_audio.mp3]` will have a corresponding `my_audio.json`).
3.  **Reviewer Loading:** When a card is shown in the reviewer, the addon looks for `.json` files corresponding to audio in the `HanziAudio`, `Example1Audio`, `Example2Audio`, and `Example3Audio` fields.
4.  **Baseline & Cents Calculation:** For each loaded JSON file, it calculates a median baseline frequency (Hz) from the data and converts all Hz values into Cents relative to that baseline (1200 cents = 1 octave). Timestamps are offset so the contour starts near time 0.
5.  **Graph Display:** The JavaScript component renders a canvas graph. The processed target contour(s) are sent to the JS.
6.  **Target Selection:** The user can select which target contour (1-4) to display. The graph's time axis adjusts to the duration of the selected target.
7.  **Live Recording:** Clicking "Record Voice" uses the SoundDevice and Aubio libraries to capture microphone input in real-time.
8.  **Live Processing:** Pitch (F0) is extracted from the live audio stream.
9.  **Live Plotting:** The detected pitch is converted to Cents (relative to a user-defined baseline in the Python code, default 150Hz) and sent to the JavaScript graph, plotted as a blue line. The time is scaled according to the selected speed multiplier (1x, 0.75x, etc.).

## Usefulness

Learning tones can be challenging without objective feedback. This addon helps by:

*   Providing a **visual representation** of pitch, making abstract tonal changes concrete.
*   Allowing direct **comparison** between a learner's pronunciation and a target (e.g., native speaker) audio sample.
*   Giving **real-time feedback** during practice within the familiar Anki environment.
*   Helping to identify specific areas where pitch accuracy needs improvement.

## How to Use

1.  **Installation:** Install like any other Anki addon.
2.  **Prerequisites:** Your notes must contain audio files (e.g., `[sound:some_audio.mp3]`) in one or more of the fields: `HanziAudio`, `Example1Audio`, `Example2Audio`, `Example3Audio`. These field names are currently hardcoded.
3.  **Generating Pitch Data (.json files):** Before the graph can display target contours, you need to process the audio files:
    *   **Bulk Process:** Go to **Tools > Tone Viewer > Bulk Process Audio...**
        *   Select the **Note Type** containing your audio.
        *   Select the specific **Audio Field(s)** you want to process (Cmd/Ctrl+Click for multiple).
        *   (Optional) Limit the number of notes to process using the "Process first" spinner for testing.
        *   (Optional) Check "Force overwrite" to re-process files even if `.json` data already exists.
        *   Click **OK**. The process runs in the background (using multiple CPU cores) and shows progress in Anki's main window title bar or status area. Hover over the '?' button for more info.
    *   **Selected Notes (Browser):** In the Anki Browser, select the notes you want to process, then go to **Edit > Tone Viewer: Process Selected Notes' Audio...**. This processes only the selected notes using the hardcoded field list.
4.  **Reviewer Interface:**
    *   The below code must be pasted into the back of your card's HTML template:
    <div id="anki_bottom">
		<!-- Addon button will be inserted here -->
	</div>
    *   When reviewing a card containing processed audio, the Tone Viewer interface will appear below the card content.
    *   **Target Buttons (1-4):** If data was found for multiple audio fields, corresponding buttons (1=HanziAudio, 2=Example1Audio, etc.) will be enabled. Click a button to display that audio's pitch contour as the orange target line. The graph's time axis will adjust.
    *   **Record Voice:** Click this button to start recording. It will turn red ("Speak to Begin").
    *   **Live Plotting:** As you speak, your pitch contour will be plotted in blue.
    *   **Speed Buttons (1x, 0.75x, 0.5x, 0.25x):** Click these to change the time scaling of the *live* (blue) plot, making it appear compressed horizontally (simulating slower playback for comparison).
    *   Recording stops automatically after a duration determined by the selected target audio, or if manually stopped (future feature).

## Dependencies & Libraries

This addon relies on several libraries:

*   **Python:** Version 3.9+ (based on Anki requirements)
*   **Aubio 0.4.9:** For pitch detection. (Vendored - included in the addon's `vendor` folder)
*   **NumPy:** Required by Aubio. (Vendored - included in the addon's `vendor` folder)
*   **SoundDevice 0.5.1:** For accessing the microphone audio stream. (CFFI backend was compiled for Python 3.9 on macOS. For other operating systems it may be easier to install a fresh version of SoundDevice. PortAudio and CFFI may also be required.)
*   **Anki / aqt / PyQt:** For addon integration and UI elements.

Add on was tested using:
*   Anki バージョン 24.06.3 (d678e393), Python 3.9.18 Qt 6.6.2 PyQt 6.6.1

*Vendoring* means that required versions of Aubio and NumPy are bundled with the addon, so you do not need to install them separately into Anki's Python environment.

## Caveats & Considerations

*   **CPU Intensive:** Bulk processing can take a significant amount of time and CPU resources, especially for large decks. However, it runs in the background.
*   **Initial Processing Required:** Target contours will only appear after you run the bulk processing or single-note processing for the relevant audio files.
*   **Hardcoded Field Names:** The addon currently only looks for audio/JSON data in fields named exactly `HanziAudio`, `Example1Audio`, `Example2Audio`, `Example3Audio`.
*   **User Baseline:** The live pitch (blue line) is converted to Cents relative to a hardcoded `USER_BASELINE_HZ` variable in `__init__.py` (currently 150.0 Hz). This should ideally be adjusted to better match the user's typical voice range for more meaningful Cents values.
*   **Pitch Detection Accuracy:** Real-time pitch detection is complex. Results can be affected by microphone quality, background noise, and the specific algorithm parameters (hop size, confidence threshold, etc.). The current settings aim for a balance between responsiveness and accuracy.
*   **Qt Warnings:** You might occasionally see Qt threading warnings in the console during bulk processing if the underlying `mp3_processor` attempts operations not safe for background processes. These are often benign but indicate areas for potential improvement in the processing code.
*   **Tooltip Delay:** The info tooltip in the bulk processing dialog appears after a standard system hover delay; it cannot be made instantaneous. 