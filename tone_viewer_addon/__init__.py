import os
import sys
import re # For regex matching of sound tags
import json # For loading model pitch data
import math # NEW: Import math for log2
import multiprocessing # NEW: For parallel processing
import functools # NEW: For partial function application
# Add addon directory to module search path
addon_path = os.path.dirname(__file__)
sys.path.insert(0, addon_path)
# Add vendor directory to module search path for vendored packages like aubio
vendor_path = os.path.join(addon_path, "vendor")
sys.path.insert(0, vendor_path)

# --- Import Core Dependencies (Aubio, NumPy) ---
aubio = None # Initialize aubio as None
np = None # Initialize np as None

try:
    import numpy as np
    print("ToneViewer Addon: NumPy imported successfully.")
except ImportError as e:
    print(f"ToneViewer Addon: Failed to import NumPy: {e}", file=sys.stderr)
    np = None # Ensure np is None if import fails

# --- Aubio Import and Test --- 
try:
    import aubio
    print(f"ToneViewer Addon: aubio module imported successfully.")
    if np:
        # --- Aubio Pitch Test (only if NumPy loaded) ---
        try:
            buf = np.zeros(512, dtype='float32') 
            pitch_o = aubio.pitch("yin", 2048, 512, 44100)
            detected_pitch = pitch_o(buf)[0]
            print(f"✅ ToneViewer Addon: aubio pitch test passed. Result: {detected_pitch}")
        except Exception as aubio_test_error:
            print(f"❌ ToneViewer Addon: aubio pitch test failed after import: {aubio_test_error}", file=sys.stderr)
        # --------------------------------------------------
    else:
        print("ToneViewer Addon: Skipping aubio test because NumPy failed to import.", file=sys.stderr)
except ImportError as e:
    print(f"ToneViewer Addon: Failed to import aubio library: {e}", file=sys.stderr)
    aubio = None 
except Exception as e:
    print(f"ToneViewer Addon: Unexpected error during aubio import/version check: {e}", file=sys.stderr)
    aubio = None 
# ---------------------------------------------------

import time
import numpy as np # Import numpy in global scope before function definitions
from typing import Union # Import Union for older Python type hints
import collections # For deque
import statistics # For median
# import numpy as np # Redundant - already imported above if aubio loaded

try:
    import sounddevice as sd  # Dependency
except ImportError as e:
    sd = None
    print(f"ToneViewer Addon: Failed to import sounddevice: {e}", file=sys.stderr)
    print("ToneViewer Addon: Please install it using: pip install sounddevice", file=sys.stderr)

from aqt import mw, QObject, pyqtSignal, gui_hooks
from aqt.reviewer import Reviewer
from aqt.utils import showInfo, showWarning, tooltip
from aqt.qt import QAction, QTimer, QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QPushButton, QComboBox, QListWidget, QListWidgetItem, QDialogButtonBox, QSpinBox, QStyle, QSizePolicy, Qt, QMessageBox # Import necessary Qt widgets
from anki.hooks import addHook, runHook
from anki.cards import Card
from aqt.browser import Browser # For browser context menu
import anki.sound # For extracting sound filenames

# --- Workaround for macOS Sonoma/Sequoia Qt tray icon bug ---
try:
    QApplication.setAttribute(QApplication.AA_DontUseNativeMenuBar, True)
    print("ToneViewer Addon: Applied AA_DontUseNativeMenuBar workaround.")
except AttributeError:
    print("ToneViewer Addon: QApplication does not have AA_DontUseNativeMenuBar (likely older Anki/Qt), skipping workaround.")
# -----------------------------------------------------------

# --- Globals ---
stream = None
listening_start_time = 0
received_chunks = 0
LISTENING_DURATION_S = 6  # Increased duration to 6 seconds
pitch_detector = None # Aubio pitch object
sample_rate = 0 # Store actual sample rate
pitch_buffer = collections.deque(maxlen=5) # Increased median filter window to 5
rms_threshold = 0.005 # Threshold for RMS energy check
last_good_raw_pitch = None # Store the last valid pitch before median filter
plotting_start_time_sec = None # Track when live plotting should start
current_target_duration_sec = LISTENING_DURATION_S # NEW: Store target duration for current card
stop_listening_timer = None # NEW: Reference to the QTimer object
USER_BASELINE_HZ = 150.0 # NEW: Placeholder baseline (Hz) for the current user - ADJUST MANUALLY
playback_speed_multiplier = 1.0 # NEW: Speed multiplier (1.0, 0.75, 0.5, 0.25)
record_button_id = "tone-viewer-record-button" # NEW: Constant for button ID

# --- Hz to Cents Conversion --- 
def hz_to_cents(f0_hz: Union[float, None], baseline_hz: float) -> Union[float, None]:
    """Converts frequency in Hz to cents relative to a baseline frequency."""
    if f0_hz is None or f0_hz <= 0 or baseline_hz <= 0:
        return None
    try:
        # 1200 * log2(f0 / baseline)
        return 1200 * math.log2(f0_hz / baseline_hz)
    except (ValueError, TypeError):
        # Handle potential math errors if inputs are unexpected types after checks
        return None
# -----------------------------

# --- Audio Processing ---

def send_pitch_to_js(time_sec: float, value_to_send: Union[float, None]) -> None:
    """Safely call the JavaScript data receiver function (expects SCALED time and cents or null)."""
    if mw and mw.reviewer and mw.reviewer.web:
        js_value = 'null' if value_to_send is None else f'{value_to_send}'
        # CALL THE RENAMED JS FUNCTION: receiveDataPoint
        # Time sent here is already scaled by the caller (audio_callback)
        js_call = f"if (window.receiveDataPoint) {{ window.receiveDataPoint({time_sec * 1000}, {js_value}); }} else {{ console.error('receiveDataPoint not defined'); }}"
        mw.reviewer.web.eval(js_call)

# NEW: Function to signal JS when to start plotting live data (with SCALED time)
def signal_start_live_plotting(start_time_sec: float) -> None:
    """Safely call the JavaScript function to signal plotting start time (using SCALED time)."""
    if mw and mw.reviewer and mw.reviewer.web:
        # Time sent here is already scaled by the caller (audio_callback)
        js_call = f"if (window.startLivePlotting) {{ window.startLivePlotting({start_time_sec * 1000}); }} else {{ console.error('startLivePlotting not defined'); }}"
        mw.reviewer.web.eval(js_call)

# NEW Helper function to start the timer on the main thread
def start_stop_timer_on_main(delay_ms: int):
    global stop_listening_timer
    print(f"ToneViewer Addon: DEBUG - start_stop_timer_on_main called on main thread with delay {delay_ms}ms.")
    if stop_listening_timer and stop_listening_timer.isActive():
        stop_listening_timer.stop()
        
    stop_listening_timer = QTimer() 
    stop_listening_timer.setSingleShot(True)
    # Connect timer timeout to the intermediate trigger function
    stop_listening_timer.timeout.connect(_trigger_stop) 
    stop_listening_timer.start(delay_ms)
    print("ToneViewer Addon: DEBUG - QTimer started on main thread, connected to _trigger_stop.")

# NEW Intermediate function called by QTimer timeout
def _trigger_stop():
    """Called by QTimer timeout signal, schedules stop_listening on main thread."""
    print("ToneViewer Addon: DEBUG - _trigger_stop called by QTimer timeout.")
    mw.taskman.run_on_main(stop_listening)

def audio_callback(indata: np.ndarray, frames: int, time_info, status: sd.CallbackFlags) -> None:
    """Called by sounddevice for each audio chunk."""
    global received_chunks, pitch_detector, listening_start_time, last_good_raw_pitch, plotting_start_time_sec, playback_speed_multiplier 
    if status:
        print(f"ToneViewer Addon: Audio Callback Status: {status}", file=sys.stderr)
    
    cents_to_send = None
    median_pitch = None
    
    # --- Calculate REAL elapsed time --- 
    real_elapsed_time_sec = time.time() - listening_start_time
    # -----------------------------------

    if pitch_detector:
        try:
            audio_chunk_float32 = indata[:, 0].astype(np.float32) 
            rms = np.sqrt(np.mean(audio_chunk_float32**2))
            
            if rms >= rms_threshold:
                pitch = pitch_detector(audio_chunk_float32)[0]
                confidence = pitch_detector.get_confidence()
                
                pitch_to_buffer = None 
                if confidence > 0.5:
                     # WIDENED ACCEPTABLE HZ RANGE
                     if 50 <= pitch <= 600: 
                         pitch_to_buffer = pitch
                         last_good_raw_pitch = pitch
                     elif last_good_raw_pitch is not None:
                         # Use last good pitch if current is out of range but confident?
                         # Consider if this logic is desired or if null should be preferred
                         # pitch_to_buffer = last_good_raw_pitch # Option: fill gaps
                         pass # Current: Treat out-of-range as effectively silent/null for buffer

                if pitch_to_buffer is not None:
                     pitch_buffer.append(pitch_to_buffer)
                     if len(pitch_buffer) > 0:
                         median_pitch = statistics.median(pitch_buffer)
                         cents_to_send = hz_to_cents(median_pitch, USER_BASELINE_HZ)
        except Exception as e:
            print(f"ToneViewer Addon: Error during pitch detection: {e}", file=sys.stderr)

    # --- Check if we should start plotting AND schedule the stop timer --- 
    if cents_to_send is not None and plotting_start_time_sec is None:
        # Store the REAL start time, but calculate the SCALED start time for JS
        plotting_start_time_sec = real_elapsed_time_sec 
        scaled_plotting_start_time_sec = plotting_start_time_sec * playback_speed_multiplier
        print(f"ToneViewer Addon: First valid pitch detected at {plotting_start_time_sec:.3f}s (real time). Sending scaled start time: {scaled_plotting_start_time_sec:.3f}s to JS.")
        # Send SCALED start time to JS
        mw.taskman.run_on_main(lambda: signal_start_live_plotting(scaled_plotting_start_time_sec))
        
        # Schedule the timer based on EFFECTIVE duration
        effective_duration_sec = current_target_duration_sec / playback_speed_multiplier
        stop_delay_ms = int(effective_duration_sec * 1000)
        print(f"ToneViewer Addon: Scheduling stop timer for {stop_delay_ms} ms (effective duration: {effective_duration_sec:.3f}s) on main thread.")
        mw.taskman.run_on_main(lambda: start_stop_timer_on_main(stop_delay_ms))
    # --------------------------------------------------------------------

    # --- Send SCALED time and CENTS data (or None) to JS --- 
    scaled_time_sec = real_elapsed_time_sec * playback_speed_multiplier
    mw.taskman.run_on_main(lambda: send_pitch_to_js(scaled_time_sec, cents_to_send))
    # -------------------------------------------------------

    received_chunks += 1

def stop_listening() -> None:
    """Stops and closes the audio stream. Should run on main thread."""
    global stream, received_chunks, pitch_detector, pitch_buffer, last_good_raw_pitch, plotting_start_time_sec, current_target_duration_sec, stop_listening_timer 
    
    print("ToneViewer Addon: stop_listening() executing on main thread.") 
    
    # Stop the QTimer if it exists and is active
    if stop_listening_timer and stop_listening_timer.isActive():
        stop_listening_timer.stop()
    stop_listening_timer = None 
    
    # Stop and close the audio stream
    if stream:
        try:
            if not stream.stopped:
                stream.stop()
                print("ToneViewer Addon: Audio stream stopped.")
            if not stream.closed:
                stream.close()
                print("ToneViewer Addon: Audio stream closed.")
            else:
                print("ToneViewer Addon: DEBUG - stream reported as already closed before close() call.")
        except AttributeError as ae:
             print(f"ToneViewer Addon: AttributeError while stopping stream (should be fixed): {ae}", file=sys.stderr)
             showWarning(f"AttributeError stopping stream: {ae}")
        except Exception as e:
            print(f"ToneViewer Addon: Error stopping stream: {e}", file=sys.stderr)
            showWarning(f"Error stopping microphone stream: {e}")
        finally:
             stream = None # Ensure stream reference is cleared
            
    # Reset only the necessary state variables for next recording
    received_chunks = 0
    pitch_detector = None 
    pitch_buffer.clear() 
    last_good_raw_pitch = None
    plotting_start_time_sec = None 
    
    tooltip("Mic test finished.") 
    # --- NEW: Reset button state ---
    if mw and mw.reviewer and mw.reviewer.web:
        try:
            mw.reviewer.web.eval(f"if(window.updateRecordButton) window.updateRecordButton(false, '{record_button_id}'); else console.error('updateRecordButton not found');")
        except Exception as e:
            print(f"ToneViewer Addon: Error calling JS to reset button: {e}", file=sys.stderr)
    # -------------------------------

def start_listening() -> None:
    """Starts the audio stream."""
    global stream, listening_start_time, received_chunks, pitch_detector, sample_rate, pitch_buffer, last_good_raw_pitch, plotting_start_time_sec, stop_listening_timer 

    # --- Ensure any existing timer is stopped --- 
    if stop_listening_timer and stop_listening_timer.isActive():
        print("ToneViewer Addon: Stopping previous timer before starting new listen.")
        stop_listening_timer.stop()
    stop_listening_timer = None
    # -----------------------------------------------

    # --- Ensure any existing stream is stopped and closed --- 
    if stream is not None and not stream.closed:
        print("ToneViewer Addon: Attempting to stop/close existing stream before starting new one...")
        try: 
            if stream.callback: stream.callback = None # Disable callback first
            if not stream.stopped: stream.stop()
            if not stream.closed: stream.close()
            print("ToneViewer Addon: Existing stream closed.")
            # --- Add a small delay --- 
            print("ToneViewer Addon: Adding small delay before starting new stream.")
            time.sleep(0.1) # 100ms delay
            # -------------------------
        except Exception as e:
             print(f"ToneViewer Addon: Error closing existing stream in start_listening: {e}")
        finally:
            stream = None # Ensure it's cleared even if errors occurred
    # -----------------------------------------------------

    # Library checks
    if not sd:
        showWarning("ToneViewer Addon: sounddevice library not found...")
        return
    if not aubio:
         showWarning("ToneViewer Addon: aubio library failed to load...")
         return

    # --- NEW: Update button state ---
    if mw and mw.reviewer and mw.reviewer.web:
        try:
            mw.reviewer.web.eval(f"if(window.updateRecordButton) window.updateRecordButton(true, '{record_button_id}'); else console.error('updateRecordButton not found');")
        except Exception as e:
            print(f"ToneViewer Addon: Error calling JS to update button: {e}", file=sys.stderr)
    # -------------------------------

    # Clear buffer and state before starting
    pitch_buffer.clear()
    last_good_raw_pitch = None
    plotting_start_time_sec = None 

    received_chunks = 0
    try:
        sd.check_input_settings() 
        print("ToneViewer Addon: Default input device seems available.")
        listening_start_time = time.time()
        print("ToneViewer Addon: Starting microphone stream...")
        try:
            device_info = sd.query_devices(kind='input') 
            sample_rate = int(device_info['default_samplerate'])
            print(f"ToneViewer Addon: Using default sample rate: {sample_rate} Hz")
        except Exception as e:
             print(f"ToneViewer Addon: Could not query sample rate, defaulting to 44100 Hz. Error: {e}")
             sample_rate = 44100
        win_s = 2048       
        hop_s = 256 # NEW: Reduced hop size for denser pitch estimates
        pitch_method = "yin" 
        try:
             pitch_detector = aubio.pitch(pitch_method, win_s, hop_s, sample_rate)
             pitch_detector.set_unit("Hz")
             pitch_detector.set_silence(-40) 
             print(f"ToneViewer Addon: Initialized aubio pitch detector ({pitch_method}, win={win_s}, hop={hop_s}, rate={sample_rate}, silence=-40dB)")
        except Exception as e:
             print(f"ToneViewer Addon: Failed to initialize aubio pitch detector: {e}", file=sys.stderr)
             pitch_detector = None 
        if not pitch_detector:
            showWarning("Failed to initialize pitch detector. Cannot start listening.")
            return

        # --- Create the new stream AFTER potential cleanup and delay ---
        stream = sd.InputStream(
            callback=audio_callback,
            channels=1,
            samplerate=sample_rate,
            blocksize=hop_s # Ensure blocksize matches hop_s
        )
        # ---------------------------------------------------------------
        stream.start()
        print(f"ToneViewer Addon: Listening started (target duration: {current_target_duration_sec:.2f}s)... Waiting for first valid pitch to start plotting.")
        tooltip("Mic test running... Speak now!")

    except sd.PortAudioError as pae:
         print(f"ToneViewer Addon: PortAudioError: {pae}", file=sys.stderr)
         if "Invalid device" in str(pae):
             showWarning("ToneViewer Addon: No microphone detected or access denied by OS.")
         elif "Device unavailable" in str(pae):
              showWarning("ToneViewer Addon: Microphone is busy or unavailable.")
         else:
              showWarning(f"ToneViewer Addon: Failed to open microphone stream: {pae}")
         stream = None 
         # --- NEW: Reset button on PortAudioError ---
         if mw and mw.reviewer and mw.reviewer.web:
              mw.reviewer.web.eval(f"if(window.updateRecordButton) window.updateRecordButton(false, '{record_button_id}');")
         # ------------------------------------------
    except Exception as e:
        print(f"ToneViewer Addon: Error starting stream: {e}", file=sys.stderr)
        showWarning(f"ToneViewer Addon: Failed to open microphone stream: {e}")
        stream = None 
        # --- NEW: Reset button on other exceptions ---
        if mw and mw.reviewer and mw.reviewer.web:
             mw.reviewer.web.eval(f"if(window.updateRecordButton) window.updateRecordButton(false, '{record_button_id}');")
        # ------------------------------------------

# --- Define JS Code Block as a standard Python string ---
_TONE_VIEWER_JS_TEMPLATE = """ 
    // --- Visualization constants ---
    const canvasWidth = 420; 
    const canvasHeight = 175; 
    let timeMaxMs = __TIME_MAX_MS__; // Default max time (can be updated by selected target)
    // Define Y-axis range in CENTS
    const centMin = -1200; // WIDENED RANGE: Min cents relative to baseline (e.g., -1 octave)
    const centMax = 1200;  // WIDENED RANGE: Max cents relative to baseline (e.g., +1 octave)
    // -----------------------------
    
    const padding = {top: 30, right: 20, bottom: 40, left: 50};
    const plotWidth = canvasWidth - padding.left - padding.right;
    const plotHeight = canvasHeight - padding.top - padding.bottom;

    // --- Global state --- 
    let liveCentsData = []; // Renamed from livePitchData
    let targetCentsData = null; // Currently selected target contour data to draw
    let allTargetDataSets = {}; // Holds data for all available targets ("1", "2", "3", "4")
    let canvasCtx = null;
    let animationFrameId = null;
    let livePlottingStartTimeMs = null; 
    // -------------------

    // Called by Python with ABSOLUTE time and LIVE cents value (or null)
    window.receiveDataPoint = function(timeMs, centsVal) { 
        if (livePlottingStartTimeMs === null) return; 
        const relativeTimeMs = timeMs - livePlottingStartTimeMs;
        // Clip live data plotting to the graph's max time
        if (relativeTimeMs < 0 || relativeTimeMs > timeMaxMs) { 
             return; 
        }
        // Store relative time and cents value
        liveCentsData.push([relativeTimeMs, centsVal]); 
    };

    // Called by Python to signal the start of live plotting
    window.startLivePlotting = function(startTimeMs) {
        if (livePlottingStartTimeMs === null) { 
            livePlottingStartTimeMs = startTimeMs;
            console.log(`ToneViewer JS: Live plotting start signaled at ${startTimeMs.toFixed(0)}ms (absolute).`);
        }
    };

    // --- NEW: Called by Python with ALL target data and the default key --- 
    window.loadAllTargetData = function(allData, defaultKey) {
        const defaultMaxMs = __TIME_MAX_MS__; // Python's original listening duration default
        console.log("ToneViewer JS: Received ALL TARGET CENTS data:", allData);
        allTargetDataSets = allData || {}; // Store the dictionary
        targetCentsData = null; // Clear current target
        timeMaxMs = defaultMaxMs; // Reset time max

        // Activate the default target if provided and valid
        if (defaultKey && allTargetDataSets[defaultKey]) {
            console.log(`ToneViewer JS: Setting default target to key: ${defaultKey}`);
            selectTargetData(defaultKey);
        } else {
            console.log("ToneViewer JS: No valid default target key provided or data missing.");
            // If no default, try selecting first available target or reset
            const firstAvailableKey = Object.keys(allTargetDataSets).sort()[0];
            if (firstAvailableKey) {
                selectTargetData(firstAvailableKey);
            } else {
                // No targets loaded at all, reset display
        targetCentsData = null; 
        timeMaxMs = defaultMaxMs; 
                updateTargetSelectorButtons(); // Update button states (all disabled)
            }
        }
    };

    // --- NEW: Function to select which target data to display --- 
    window.selectTargetData = function(targetKey) {
        const defaultMaxMs = __TIME_MAX_MS__;
        const dataSet = allTargetDataSets[targetKey];

        // --- NEW: Clear previous live recording data --- 
        liveCentsData = [];
        livePlottingStartTimeMs = null; // Also reset the live plotting start time
        console.log("ToneViewer JS: Cleared live recording data.");
        // -----------------------------------------------

        if (dataSet && dataSet.cents && dataSet.duration_sec !== undefined) {
            console.log(`ToneViewer JS: Selecting target data for key: ${targetKey}`);
            targetCentsData = dataSet.cents;
            // Update timeMaxMs based on the selected target's duration
            timeMaxMs = dataSet.duration_sec * 1000;
            if (timeMaxMs <= 0) { // Fallback if duration is zero or negative
                timeMaxMs = defaultMaxMs;
            }
            console.log(`ToneViewer JS: Updated timeMaxMs to: ${timeMaxMs.toFixed(0)}ms for target ${targetKey}`);
        } else {
            console.warn(`ToneViewer JS: No valid data found for target key: ${targetKey}. Clearing target display.`);
            targetCentsData = null;
            timeMaxMs = defaultMaxMs; // Reset to default listening duration
        }
        
        updateTargetSelectorButtons(targetKey); // Update button highlighting
        // Trigger a redraw immediately to show the cleared live data and new target/scale
        if (canvasCtx && !animationFrameId) {
            // If animation isn't running, request a frame. Otherwise, it will update.
             drawPitchContour(); // Or requestAnimationFrame(drawPitchContour) if needed
        }
    };

    // --- NEW: Helper to update target selector button styles --- 
    function updateTargetSelectorButtons(activeKey = null) {
        const container = document.getElementById('tone-viewer-target-selector');
        if (!container) return;
        const buttons = container.querySelectorAll('button');
        buttons.forEach(btn => {
            const key = btn.getAttribute('data-target-key');
            const hasData = allTargetDataSets[key] !== undefined;
            btn.disabled = !hasData;
            if (hasData && key === activeKey) {
                btn.style.backgroundColor = '#a6d8a6'; // Light green for active
                btn.style.fontWeight = 'bold';
            } else if (hasData) {
                btn.style.backgroundColor = ''; // Default for available but inactive
                btn.style.fontWeight = 'normal';
        } else {
                btn.style.backgroundColor = '#f0f0f0'; // Grey out disabled
                btn.style.fontWeight = 'normal';
            }
        });
    }
    // ---------------------------------------------------------
    
    // --- NEW: Function to update the record button state --- 
    window.updateRecordButton = function(isRecording, buttonId) {
        const button = document.getElementById(buttonId);
        if (!button) {
            console.error('ToneViewer JS: Record button not found with ID:', buttonId);
            return;
        }
        if (isRecording) {
            button.innerText = 'Speak to Begin';
            button.style.backgroundColor = '#dc3545'; // Red color
            button.style.color = 'white';
            button.disabled = true; // Disable while recording
        } else {
            button.innerText = 'Record Voice';
            button.style.backgroundColor = ''; // Default background
            button.style.color = ''; // Default text color
            button.disabled = false; // Re-enable
        }
    };
    // -----------------------------------------------------
    
    // Helper to map data coords (time, CENTS) to canvas coords
    function mapCoords(timeMs, centsVal) {
        if (timeMaxMs <= 0) return { x: -1, y: -1 }; 
        
        let clampedCents = (centsVal === null || centsVal === undefined) 
                            ? null 
                            : Math.max(centMin, Math.min(centMax, centsVal));
                            
        if (clampedCents === null) {
             return { x: -1, y: -1}; 
        }

        let timeRatio = timeMs / timeMaxMs; 
        let x = padding.left + timeRatio * plotWidth;
        let y = padding.top + plotHeight - ((clampedCents - centMin) / (centMax - centMin)) * plotHeight; 
        
        if (!isFinite(x) || !isFinite(y)) {
             return { x: -1, y: -1 }; 
        }
        return { x, y };
    }

    // Drawing function (updated for CENTS)
    function drawPitchContour() {
        if (!canvasCtx) { return; }
        const ctx = canvasCtx;
        
        ctx.fillStyle = 'white';
        ctx.fillRect(0, 0, canvasWidth, canvasHeight);
        ctx.strokeStyle = '#ddd'; 
        ctx.lineWidth = 1;
        ctx.strokeRect(padding.left, padding.top, plotWidth, plotHeight);

        // --- Grid Lines (Y-axis updated for CENTS) --- 
        ctx.strokeStyle = '#eee'; 
        ctx.lineWidth = 1;
        ctx.font = '10px sans-serif';
        ctx.fillStyle = '#666'; 
        ctx.textAlign = 'right'; 
        ctx.textBaseline = 'middle';
        const numHorizGridLines = 10; 
        for (let i = 0; i <= numHorizGridLines; i++) {
            let cents = centMin + i * (centMax - centMin) / numHorizGridLines;
            let mapped = mapCoords(0, cents); 
            if (mapped.y < 0) continue; 
            ctx.beginPath();
            ctx.moveTo(padding.left, mapped.y);
            ctx.lineTo(padding.left + plotWidth, mapped.y);
            ctx.stroke();
            let label = (cents > 0 ? '+' : '') + cents.toFixed(0);
            ctx.fillText(label, padding.left - 5, mapped.y); 
        }
        // Vertical grid lines & X-axis labels (Time - unchanged, uses dynamic timeMaxMs)
        const vertGridInterval = 200; 
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        for (let time = vertGridInterval; time < timeMaxMs; time += vertGridInterval) {
            let mapped = mapCoords(time, centMin); 
            if (mapped.x < 0) continue; 
            ctx.beginPath();
            ctx.moveTo(mapped.x, padding.top);
            ctx.lineTo(mapped.x, padding.top + plotHeight);
            ctx.stroke();
            if (mapped.x > padding.left + 10) {
                 ctx.fillText(time.toFixed(0), mapped.x, padding.top + plotHeight + 5);
            }
        }
        // Final X-axis label (Time - refined, uses dynamic timeMaxMs)
        if (timeMaxMs > 0) {
            let finalMapped = mapCoords(timeMaxMs, centMin);
            let lastIntervalTime = Math.floor((timeMaxMs - 1) / vertGridInterval) * vertGridInterval;
            let lastIntervalMapped = mapCoords(lastIntervalTime, centMin);
            // Only draw if it doesn't overlap too much with the previous label
            if (finalMapped.x > padding.left && (lastIntervalTime <= 0 || finalMapped.x > lastIntervalMapped.x + 20)) {
                 ctx.beginPath();
                 ctx.moveTo(finalMapped.x, padding.top);
                 ctx.lineTo(finalMapped.x, padding.top + plotHeight); 
                 ctx.stroke();
                 ctx.fillText(timeMaxMs.toFixed(0), finalMapped.x, padding.top + plotHeight + 5);
            }
        }
        // ------------------------------------------------
        
        // --- Axis Labels and Title (Y-label updated) --- 
        ctx.fillStyle = 'black'; 
        ctx.textAlign = 'center';
        ctx.font = '14px sans-serif';
        // Center the title relative to the plot area and adjust vertical padding
        ctx.fillText('Pitch Contour', padding.left + plotWidth / 2, padding.top / 6); // Adjusted y position
        ctx.font = '12px sans-serif';
        ctx.fillText('Time (ms)', padding.left + plotWidth / 2, canvasHeight - padding.bottom / 2.5);
        ctx.save(); 
        ctx.translate(padding.left / 12, padding.top + plotHeight / 2); // Adjusted x translation
        ctx.rotate(-Math.PI / 2);
        ctx.fillText('Pitch (cents)', 0, 0); // UPDATED Y-AXIS LABEL
        ctx.restore(); 
        // -----------------------------------------------

        // --- Draw TARGET Cents Contour (Orange, uses targetCentsData) --- 
        if (targetCentsData && targetCentsData.length > 0) {
            ctx.strokeStyle = '#FFA500'; 
            ctx.fillStyle = '#FFA500';   
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            let targetLastValid = false;
            for (let i = 0; i < targetCentsData.length; i++) {
                let timeMs = targetCentsData[i][0]; 
                let centsVal = targetCentsData[i][1]; 
                
                if (centsVal !== null) {
                    let mapped = mapCoords(timeMs, centsVal);
                    if (mapped.x >= 0) { 
                        if (targetLastValid) {
                            ctx.lineTo(mapped.x, mapped.y);
                        } else {
                            ctx.moveTo(mapped.x, mapped.y);
                        }
                        targetLastValid = true;
                        // Draw small circle for target points
                        ctx.beginPath(); 
                        ctx.arc(mapped.x, mapped.y, 2, 0, 2 * Math.PI);
                        ctx.fill();
                        // Reset path start for next segment
                        ctx.beginPath(); 
                        ctx.moveTo(mapped.x, mapped.y);
                    } else {
                        targetLastValid = false; 
                    }
                } else {
                    targetLastValid = false; 
                }
            }
             // Stroke the line segments connecting valid points
            ctx.stroke(); 
        }
        // ---------------------------------------------
        
        // --- Draw LIVE Cents Contour (Blue) --- 
        if (liveCentsData.length >= 1) { 
             ctx.strokeStyle = '#0000FF'; 
             ctx.fillStyle = '#0000FF';   
             ctx.lineWidth = 2;
             ctx.beginPath(); 
             let liveLastValid = false;
             for (let i = 0; i < liveCentsData.length; i++) {
                 let timeMs = liveCentsData[i][0]; 
                 let centsVal = liveCentsData[i][1]; 
                 
                 if (centsVal !== null) {
                     let mapped = mapCoords(timeMs, centsVal);
                     if (mapped.x >= 0) {
                         if (liveLastValid) {
                             ctx.lineTo(mapped.x, mapped.y);
                         } else {
                             ctx.moveTo(mapped.x, mapped.y);
                         }
                         liveLastValid = true;
                         // Draw slightly larger circle for live points
                         ctx.beginPath(); 
                         ctx.arc(mapped.x, mapped.y, 2.5, 0, 2 * Math.PI);
                         ctx.fill();
                          // Reset path start for next segment
                         ctx.beginPath(); 
                         ctx.moveTo(mapped.x, mapped.y);
                     } else {
                         liveLastValid = false;
                     }
                 } else {
                      liveLastValid = false;
                 }
             }
             // Stroke the line segments connecting valid points
             ctx.stroke();
        }
        // -----------------------------------------
        animationFrameId = requestAnimationFrame(drawPitchContour);
    };

    // Function to setup and start everything 
    function setupVisualizer(container) {
        console.log('ToneViewer JS: Setting up visualizer.');
        if (animationFrameId) {
            cancelAnimationFrame(animationFrameId);
            animationFrameId = null;
        }
        liveCentsData = []; 
        livePlottingStartTimeMs = null; 
        var canvasId = 'tone-viewer-pitch-canvas';
        var canvas = document.getElementById(canvasId);
        if (!canvas) {
            console.log('ToneViewer JS: Creating canvas #' + canvasId);
            canvas = document.createElement('canvas');
            canvas.id = canvasId;
            canvas.width = canvasWidth;   
            canvas.height = canvasHeight; 
            canvas.style.marginTop = '10px';
            container.appendChild(canvas);
        } else {
             console.log('ToneViewer JS: Found existing canvas #' + canvasId);
             // Ensure canvas dimensions are reset if needed
             canvas.width = canvasWidth;
             canvas.height = canvasHeight;
             canvasCtx = null; // Force getting context again
        }
        canvasCtx = canvas.getContext('2d');
        if (canvasCtx) {
             console.log('ToneViewer JS: Got canvas context. Starting animation frame.');
             // Don't start animation immediately, wait for data?
             // animationFrameId = requestAnimationFrame(drawPitchContour);
             drawPitchContour(); // Draw initial state (empty graph with axes)
        } else {
            console.error('ToneViewer JS: Failed to get 2D context for canvas.');
        }
    };

    // Main function called by Python hook (creates button and canvas)
    function tryAddButton() {
        console.log('ToneViewer JS: tryAddButton() called by Python hook.');
        var backTemplateAnchor = document.getElementById('anki_bottom');
        if (!backTemplateAnchor) {
            console.log('ToneViewer JS: Anchor element #anki_bottom not found.');
            return;
        }
        console.log('ToneViewer JS: Anchor element #anki_bottom found. Proceeding...');
        
        var containerId = 'tone-viewer-button-container';
        var container = document.getElementById(containerId);
        if (!container) {
            console.log('ToneViewer JS: Creating container #' + containerId);
            container = document.createElement('div');
            container.id = containerId;
            // Insert before the anchor if the anchor exists
            if (backTemplateAnchor.parentNode) {
            backTemplateAnchor.parentNode.insertBefore(container, backTemplateAnchor);
            } else {
                 // Fallback: append to body? Might not be ideal.
                 document.body.appendChild(container);
                 console.warn('ToneViewer JS: #anki_bottom anchor has no parent, appending container to body.');
            }
        } else {
             console.log('ToneViewer JS: Found existing container #' + containerId);
             // Clear previous content to avoid duplicate buttons/canvas on redraw
             container.innerHTML = ''; 
        }

        // Create the button and canvas elements INSIDE this function
        try { 
            // --- Record Voice Button --- 
            var button_id_js = '__BUTTON_ID__'; 
            var recordBtn = document.createElement('button');
            recordBtn.id = button_id_js;
            recordBtn.innerText = 'Record Voice';
            recordBtn.style.marginRight = '10px';
            recordBtn.onclick = function() { 
                // Don't setup visualizer here, just send command?
                // Or ensure setupVisualizer redraws correctly
                setupVisualizer(container); // Reset visuals if needed
                pycmd('tone_viewer_start'); 
                return false; 
            };
            container.appendChild(recordBtn);
            console.log('ToneViewer JS: Added Record Voice button to: #' + container.id);

            // --- Target Selector Buttons (1, 2, 3, 4) --- 
            const targetSelectorContainer = document.createElement('div');
            targetSelectorContainer.id = 'tone-viewer-target-selector';
            targetSelectorContainer.style.display = 'inline-block';
            targetSelectorContainer.style.marginLeft = '15px';
            container.appendChild(targetSelectorContainer);

            const targetLabel = document.createElement('span');
            targetLabel.innerText = 'Target:';
            targetLabel.style.marginRight = '5px';
            targetSelectorContainer.appendChild(targetLabel);

            for (let i = 1; i <= 4; i++) {
                const targetBtn = document.createElement('button');
                const targetKey = String(i);
                targetBtn.innerText = targetKey;
                targetBtn.setAttribute('data-target-key', targetKey);
                targetBtn.style.marginLeft = '2px';
                targetBtn.style.minWidth = '25px';
                targetBtn.disabled = true; // Initially disabled
                targetBtn.onclick = function() {
                    selectTargetData(this.getAttribute('data-target-key'));
                    return false;
                };
                targetSelectorContainer.appendChild(targetBtn);
            }
            console.log('ToneViewer JS: Added target selector buttons [1, 2, 3, 4].');

            // --- Speed Control Buttons --- 
            const speeds = [1.0, 0.75, 0.5, 0.25];
            const speedButtonContainer = document.createElement('span');
            speedButtonContainer.style.marginLeft = '15px';
            container.appendChild(speedButtonContainer);
            
            // Helper function to update speed button styles
            window.updateActiveSpeedButton = function(activeSpeed) {
                const buttons = speedButtonContainer.querySelectorAll('button');
                buttons.forEach(btn => {
                    if (parseFloat(btn.getAttribute('data-speed')) === activeSpeed) {
                        btn.style.backgroundColor = '#cce5ff';
                        btn.style.fontWeight = 'bold';
                    } else {
                        btn.style.backgroundColor = '';
                        btn.style.fontWeight = 'normal';
                    }
                });
            }

            speeds.forEach(speed => {
                const speedBtn = document.createElement('button');
                speedBtn.innerText = `${speed}x`;
                speedBtn.setAttribute('data-speed', speed);
                speedBtn.style.marginLeft = '5px';
                speedBtn.onclick = function() {
                    const speedValue = this.getAttribute('data-speed');
                    pycmd(`tone_viewer_set_speed_${speedValue}`);
                    window.updateActiveSpeedButton(parseFloat(speedValue)); 
                    return false;
                };
                speedButtonContainer.appendChild(speedBtn);
            });
            
            // Initially highlight the 1.0x button
            window.updateActiveSpeedButton(1.0);
            console.log('ToneViewer JS: Added speed control buttons.');
            
            // Setup the visualizer canvas (needs container)
            setupVisualizer(container); 

            // Update target buttons based on initially loaded data (might be empty initially)
            updateTargetSelectorButtons(); 

        } catch (e) {
            console.error('ToneViewer JS: Error during button/canvas creation:', e);
        }
    }; 
"""
# ----------------------------------------------------

def get_try_add_button_js(button_id):
    """Injects button_id and LISTENING_DURATION_S into the JS template."""
    # Calculate timeMaxMs from Python constant
    time_max_ms_val = LISTENING_DURATION_S * 1000
    # Replace placeholders in the template string
    js_code = _TONE_VIEWER_JS_TEMPLATE.replace('__TIME_MAX_MS__', str(time_max_ms_val))
    js_code = js_code.replace('__BUTTON_ID__', button_id)
    return js_code

def inject_button_js_definition(web_content, context):
    """Injects the JavaScript function definition into the webview."""
    if isinstance(context, Reviewer):
        print("ToneViewer Addon: Injecting JS function definition.")
        # Use the globally defined button ID
        js_definition = get_try_add_button_js(record_button_id) 
        web_content.body += f"<script>{js_definition}</script>"
    return web_content

# New function triggered after answer is shown
def on_show_answer(card: Card) -> None:
    """Loads model Hz data for multiple fields, calculates target baseline and cents, sends all data to JS."""
    global current_target_duration_sec, LISTENING_DURATION_S
    reviewer = mw.reviewer
    if not reviewer: return
    print("ToneViewer Addon: reviewer_did_show_answer hook fired.")

    all_targets_data = {} # Dictionary to hold data for targets "1" through "4"
    field_names = ["HanziAudio", "Example1Audio", "Example2Audio", "Example3Audio"]
    default_target_key = None # Which target to display initially ("1", "2", "3", or "4")
    default_duration_sec = LISTENING_DURATION_S # Fallback duration

    for index, field_name in enumerate(field_names):
        target_key = str(index + 1) # Key will be "1", "2", "3", "4"
        original_pitch_data = None
        target_cents_data = None
        target_duration_sec = None
        target_baseline_hz = None

        if field_name in card.note():
            field_value = card.note()[field_name]
            filenames = re.findall(r"\[sound:(.*?\.mp3)]", field_value, re.IGNORECASE)
            if filenames:
                mp3_filename = filenames[0]
                json_filename = mp3_filename.replace('.mp3', '.json').replace('.MP3', '.json')
                json_path = os.path.join(mw.col.media.dir(), json_filename)
                
                if os.path.exists(json_path):
                    try:
                        with open(json_path, 'r') as f:
                            original_pitch_data = json.load(f)
                        print(f"ToneViewer Addon: [{target_key}] Loaded model pitch data (Hz) from {json_filename}")
                    
                        if original_pitch_data and isinstance(original_pitch_data, list) and len(original_pitch_data) > 1:
                            # --- Calculate Target Baseline Hz --- 
                            hz_values = [p[1] for p in original_pitch_data if p[1] is not None and p[1] > 0]
                            if hz_values:
                                target_baseline_hz = statistics.median(hz_values)
                                print(f"ToneViewer Addon: [{target_key}] Calculated Target Baseline: {target_baseline_hz:.2f} Hz")
                            else:
                                print(f"ToneViewer Addon: [{target_key}] No valid Hz values, using default baseline 120 Hz.")
                                target_baseline_hz = 120.0 # Default fallback
                        
                            # --- Convert Target Hz to Cents & Offset Time --- 
                            first_ts = original_pitch_data[0][0]
                            last_ts = original_pitch_data[-1][0]
                            target_cents_data = [] # Reset list
                            for ts, hz in original_pitch_data:
                                cents = hz_to_cents(hz, target_baseline_hz)
                                offset_ts = ts - first_ts # Keep in ms for JS
                                target_cents_data.append([offset_ts, cents]) # Store offset_ts (ms), cents
                         
                            # --- Calculate duration from OFFSET timestamps --- 
                            target_duration_sec = max(0, (last_ts - first_ts) / 1000.0) # Ensure non-negative
                            print(f"ToneViewer Addon: [{target_key}] Calculated target duration: {target_duration_sec:.3f}s")
                           
                            # Store processed data for this target
                            all_targets_data[target_key] = {
                                "cents": target_cents_data,
                                "duration_sec": target_duration_sec
                            }
                           
                            # Set default target (prefer Example1Audio, otherwise first found)
                            if field_name == "Example1Audio":
                                default_target_key = target_key
                                default_duration_sec = target_duration_sec
                            elif default_target_key is None:
                                default_target_key = target_key
                                default_duration_sec = target_duration_sec
                               
                        else:
                            print(f"ToneViewer Addon: [{target_key}] Model data invalid/too short in {json_filename}.")
                        
                    except Exception as e:
                        print(f"ToneViewer Addon: [{target_key}] Error loading/processing JSON {json_filename}: {e}", file=sys.stderr)
                else: 
                    print(f"ToneViewer Addon: [{target_key}] Model JSON not found: {json_filename}")
            else:
                print(f"ToneViewer Addon: [{target_key}] No MP3 sound tag found in field '{field_name}'")
        else:
             print(f"ToneViewer Addon: [{target_key}] Field '{field_name}' not found in note")

    # Set the global duration based on the default target, or fallback
    current_target_duration_sec = default_duration_sec
    print(f"ToneViewer Addon: Setting initial target duration to: {current_target_duration_sec:.3f}s (based on default: {default_target_key})")

    # --- Call JS Functions --- 
    try:
        # Send ALL processed target data (dictionary) and the default key to JS
        js_data_string = json.dumps(all_targets_data) 
        js_default_key = json.dumps(default_target_key) # Send as string or null
        
        # Use a NEW JS function name for clarity
        js_call_targets = f"if (window.loadAllTargetData) {{ window.loadAllTargetData({js_data_string}, {js_default_key}); }} else {{ console.error('loadAllTargetData not defined'); }}"
        reviewer.web.eval(js_call_targets)
            
        reviewer.web.eval("tryAddButton();") # Keep this to create buttons/canvas
        print("ToneViewer Addon: Called JS loadAllTargetData and tryAddButton.")
    except Exception as e:
        print(f"ToneViewer Addon: Error calling reviewer.web.eval in on_show_answer: {e}")

# --- New pycmd Handling (Anki >= 2.1.50) ---
def handle_js_message(handled: tuple[bool, object], message: str, context: object) -> tuple[bool, object]:
    """Listens for pycmd messages from the webview."""
    global playback_speed_multiplier # Make sure global is accessible
    
    if message == "tone_viewer_start":
        start_listening()
        return (True, None)
    # --- NEW: Handle Speed Control Messages --- 
    elif message.startswith("tone_viewer_set_speed_"):
        try:
            speed_str = message.replace("tone_viewer_set_speed_", "")
            new_speed = float(speed_str)
            if new_speed in [1.0, 0.75, 0.5, 0.25]:
                playback_speed_multiplier = new_speed
                print(f"ToneViewer Addon: Set playback speed multiplier to {playback_speed_multiplier}")
                tooltip(f"Speed set to {playback_speed_multiplier}x")
                # Optionally, send a message back to JS to update UI indication
                # js_call = f"if(window.updateActiveSpeedButton) window.updateActiveSpeedButton({playback_speed_multiplier});"
                # if mw and mw.reviewer and mw.reviewer.web:
                #     mw.reviewer.web.eval(js_call)
                return (True, None)
            else:
                 print(f"ToneViewer Addon: Invalid speed value received: {new_speed}", file=sys.stderr)
                 return (True, None) # Handled, but invalid value
        except ValueError:
             print(f"ToneViewer Addon: Could not parse speed value from message: {message}", file=sys.stderr)
             return (True, None) # Handled, but invalid format
    # ----------------------------------------
    return handled

# --- Hook Registrations ---

# Hook to handle pycmd calls from JS
gui_hooks.webview_did_receive_js_message.append(handle_js_message)
print("ToneViewer Addon: Registered JS message handler.")

# Hook to INJECT JS function definition when web content is set
gui_hooks.webview_will_set_content.append(inject_button_js_definition)
print("ToneViewer Addon: Registered webview_will_set_content hook (for JS definition)." + "\n" + "hello")

# Hook to CALL the JS function after the answer is shown
gui_hooks.reviewer_did_show_answer.append(on_show_answer)
print("ToneViewer Addon: Registered reviewer_did_show_answer hook (to call JS)." + "\n" + "hello")

print("ToneViewer Addon: Loaded successfully.")

# Clean up stream on Anki close? Not strictly necessary for this test.
# def on_profile_will_close():
#     print("ToneViewer Addon: Cleaning up stream on profile close.")
#     stop_listening()
# addHook("profile_will_close", on_profile_will_close) 

# Make sure mp3_processor functions can be imported
try:
    from . import mp3_processor
    print("ToneViewer Addon: MP3 processor module imported.")
except ImportError as e:
    mp3_processor = None
    print(f"ToneViewer Addon: Failed to import mp3_processor module: {e}", file=sys.stderr)

# --- Bulk Processing Dialog --- 

class BulkProcessDialog(QDialog):
    # Store help text as a class variable for reuse
    _info_text = ( 
        "Process audio files in selected fields for a chosen note type "
        "to generate pitch data (.json files) used by the Tone Viewer graph.\n\n"
        "Select the note type and the audio field(s) containing the relevant MP3s. "
        "You can optionally limit the number of notes processed for testing. "
        "The 'Force Overwrite' option will re-process files even if JSON already exists.\n\n"
        "Note: This can be very time-consuming and CPU-intensive for large numbers of notes, "
        "but it runs in the background so you can continue using Anki."
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mw = parent
        self.setWindowTitle("Tone Viewer - Bulk Process Audio")
        self.setMinimumWidth(400)

        # --- Info Button (Create first for layout) ---
        info_button = QPushButton()
        try:
            info_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxQuestion)
            info_button.setIcon(info_icon)
        except Exception as e:
            info_button.setText("?")
            print(f"Could not load standard icon: {e}")
        info_button.setToolTip(self._info_text)
        # Styling for info button (?)
        button_size = info_button.iconSize().height() * 1.2
        info_button.setFixedSize(button_size, button_size)
        radius = button_size / 2
        # More aggressive stylesheet for circle + slight top margin
        info_button.setStyleSheet(
            f"QPushButton {{ "
            f"border-radius: {radius}px; "
            f"border: none; "
            f"padding: 0px; "
            f"margin-top: 2px; " # Nudge down slightly
            f"}}"
        )
        info_button.clicked.connect(self._show_info_message)

        # --- Main Vertical Layout ---
        main_layout = QVBoxLayout(self)

        # --- Top Row Layout (Label + Info Button) ---
        top_hbox = QHBoxLayout()
        label_notetype = QLabel("1. Select Note Type:")
        # Ensure both widgets use AlignVCenter
        top_hbox.addWidget(label_notetype, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        top_hbox.addStretch()
        top_hbox.addWidget(info_button, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        main_layout.addLayout(top_hbox)

        # --- Note Type Selector ---
        self.note_type_combo = QComboBox()
        self.note_types = self.mw.col.models.all()
        for nt in self.note_types:
            self.note_type_combo.addItem(nt['name'], nt['id'])
        self.note_type_combo.currentIndexChanged.connect(self._update_field_list)
        main_layout.addWidget(self.note_type_combo)

        # --- Note Count Display ---
        self.count_label = QLabel("Total notes found: N/A")
        main_layout.addWidget(self.count_label)

        # --- Field Selector (Renumbered) ---
        main_layout.addWidget(QLabel("2. Select Audio Field(s): (Cmd/Ctrl+Click for multiple)"))
        self.field_list = QListWidget()
        self.field_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        main_layout.addWidget(self.field_list)

        # --- Process Count Selector (Renumbered) ---
        count_hbox = QHBoxLayout()
        count_hbox.addWidget(QLabel("3. Process first:"))
        self.count_spinbox = QSpinBox()
        self.count_spinbox.setMinimum(1)
        self.count_spinbox.setMaximum(1)
        self.count_spinbox.setValue(1)
        self.count_spinbox.setEnabled(False)
        count_hbox.addWidget(self.count_spinbox)
        count_hbox.addWidget(QLabel("notes"))
        count_hbox.addStretch()
        main_layout.addLayout(count_hbox)

        # --- Options (Renumbered) ---
        main_layout.addWidget(QLabel("4. Options:"))
        self.force_checkbox = QCheckBox("Force overwrite existing JSON files")
        main_layout.addWidget(self.force_checkbox)
        
        # --- Buttons ---
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        main_layout.addWidget(self.button_box)

        # --- Initial Population ---
        self._update_field_list()

    def _show_info_message(self):
        """Displays the help text in a message box when the info button is clicked."""
        QMessageBox.information(self, "Bulk Process Audio Info", self._info_text)

    def _update_field_list(self):
        """Called when the selected note type changes."""
        current_nt_id = self.note_type_combo.currentData()
        if not current_nt_id:
            self.count_label.setText("Total notes found: N/A")
            self.count_spinbox.setEnabled(False)
            self.field_list.clear()
            return
        
        # Update Field List
        note_type = self.mw.col.models.get(current_nt_id)
        self.field_list.clear()
        if note_type:
            for field in note_type['flds']:
                item = QListWidgetItem(field['name'])
                self.field_list.addItem(item)

        # Update Note Count and Spinbox
        try:
            nids = self.mw.col.models.nids(current_nt_id)
            total_notes = len(nids)
            self.count_label.setText(f"Total notes found: {total_notes}")
            
            if total_notes > 0:
                self.count_spinbox.setEnabled(True)
                self.count_spinbox.setMaximum(total_notes)
                self.count_spinbox.setValue(min(100, total_notes)) # Default to 100 or total
            else:
                self.count_spinbox.setEnabled(False)
                self.count_spinbox.setMaximum(1)
                self.count_spinbox.setValue(1)

        except Exception as e:
            print(f"Error counting notes for note type {current_nt_id}: {e}")
            self.count_label.setText("Total notes found: Error")
            self.count_spinbox.setEnabled(False)

    def accept(self):
        # Get selected note type ID
        selected_nt_id = self.note_type_combo.currentData()
        selected_nt_name = self.note_type_combo.currentText()
        
        # Get selected field names
        selected_fields = [item.text() for item in self.field_list.selectedItems()]

        # Get force overwrite setting
        force_overwrite = self.force_checkbox.isChecked()

        if not selected_nt_id:
            showWarning("Please select a note type.")
            return
        if not selected_fields:
            showWarning("Please select at least one audio field.")
            return

        print(f"  Note Type: {selected_nt_name} (ID: {selected_nt_id})")
        print(f"  Fields: {selected_fields}")
        print(f"  Force Overwrite: {force_overwrite}")

        # --- NEW: Get max count from spinbox ---
        max_notes_to_process = self.count_spinbox.value()
        print(f"  Processing max: {max_notes_to_process} notes")
        # ----------------------------------------

        # --- NEW: Gather tasks and run in background ---
        if not mp3_processor:
            showWarning("MP3 Processor module failed to load. Cannot process audio.")
            return
        if not mw.col or not mw.col.media:
            showWarning("Collection or media collection not available.")
            return
            
        media_dir = mw.col.media.dir()
        tasks = []
        nids_all = self.mw.col.models.nids(selected_nt_id)
        # --- NEW: Slice the nids list based on spinbox value ---
        nids_to_process = nids_all[:max_notes_to_process]
        # ----------------------------------------------------
        print(f"Found {len(nids_all)} total notes for type '{selected_nt_name}'. Gathering audio files from first {len(nids_to_process)}...")

        # Show preliminary progress while gathering tasks (removed immediate=True)
        self.mw.progress.start(label="Finding audio files...")
        tasks_gathered = 0
        for i, nid in enumerate(nids_to_process):
            if i % 100 == 0: # Update progress periodically
                self.mw.progress.update(label=f"Finding audio files... ({i}/{len(nids_to_process)})")
                if self.mw.progress.want_cancel(): 
                    self.mw.progress.finish()
                    tooltip("Bulk process cancelled during file gathering.")
                    return
            
            note = self.mw.col.get_note(nid)
            if not note:
                continue
            
            for field_name in selected_fields:
                if field_name in note:
                    field_value = note[field_name]
                    # Find all sound tags in the field using regex
                    for filename in re.findall(r"\[sound:(.*?)(\.mp3)]", field_value, re.IGNORECASE):
                        mp3_filename = filename[0] + filename[1] # Combine parts from regex
                        if not mp3_filename.lower().endswith('.mp3'):
                             continue # Should be caught by regex, but double-check
                            
                        mp3_path = os.path.join(media_dir, mp3_filename)
                        json_filename = mp3_filename.replace('.mp3', '.json').replace('.MP3', '.json')
                        json_path = os.path.join(media_dir, json_filename)
                        
                        # Add task info tuple: (nid, field_name, mp3_filename, mp3_path, json_path)
                        tasks.append((nid, field_name, mp3_filename, mp3_path, json_path))
        
        self.mw.progress.finish() # Finish finding files progress

        if not tasks:
            showInfo(f"No MP3 files found in the selected fields ({', '.join(selected_fields)}) for note type '{selected_nt_name}'.")
            return
            
        print(f"Found {len(tasks)} MP3 files to process. Starting background task...")
        
        # Launch the actual processing in the background
        mw.taskman.run_in_background(
            lambda: _run_bulk_processing(tasks, force_overwrite),
            None # No callback needed on completion here, _run_bulk_processing shows tooltip
        )
        # ------------------------------------------------

        super().accept()

# Function to show the dialog
def show_bulk_process_dialog():
    if not mw:
        tooltip("Anki main window not available.")
        return
    # TODO: Check if mp3_processor loaded correctly?
    dialog = BulkProcessDialog(mw) # Pass mw as parent
    # TODO: Populate initial dialog values (note types)
    dialog.exec() # Show the dialog modally

# --- Single Note Processing (for testing) ---

# Define the fields to check for audio
FIELDS_TO_PROCESS = ["HanziAudio", "Example1Audio", "Example2Audio", "Example3Audio"]

def process_single_note_audio_action(browser: Browser):
    """Action triggered from the browser menu to process selected notes' audio."""
    nids = browser.selected_notes()
    if not nids:
        tooltip("No notes selected.")
        return
    if not mp3_processor:
        showWarning("MP3 Processor module failed to load. Cannot process audio.")
        return
    if not mw.col.media:
        showWarning("Media collection not available.")
        return

    processed_files = 0
    failed_files = 0
    skipped_files = 0
    media_dir = mw.col.media.dir()

    # Use progress indicator
    mw.progress.start(label="Processing selected notes' audio...", max=len(nids), immediate=True)

    for i, nid in enumerate(nids):
        mw.progress.update(value=i)
        if mw.progress.want_cancel(): break
        
        note = mw.col.get_note(nid)
        if not note:
            continue
            
        note_processed_at_least_one = False
        for field_name in FIELDS_TO_PROCESS:
            if field_name in note:
                field_value = note[field_name]
                # Find all sound tags in the field using regex
                for filename in re.findall(r"\[sound:(.*?)(\.mp3)]", field_value, re.IGNORECASE):
                    if not filename.lower().endswith('.mp3'):
                        continue # Skip non-mp3 files
                        
                    mp3_path = os.path.join(media_dir, filename)
                    json_filename = filename.replace('.mp3', '.json').replace('.MP3', '.json')
                    json_path = os.path.join(media_dir, json_filename)
                    
                    if not os.path.exists(mp3_path):
                        print(f"Skipping {filename}: MP3 file not found at {mp3_path}")
                        skipped_files += 1
                        continue
                    
                    # Call the processor (runs synchronously for this simple action)
                    # TODO: Consider background thread even for this if it blocks too long?
                    try:
                        success = mp3_processor.process_mp3_to_json(mp3_path, json_path)
                        if success:
                            processed_files += 1
                            note_processed_at_least_one = True
                        else:
                            failed_files += 1
                    except Exception as e:
                         print(f"Error processing {filename} for note {nid}: {e}", file=sys.stderr)
                         failed_files += 1

    mw.progress.finish()
    tooltip(f"Processed {processed_files} files. Failed/Skipped: {failed_files + skipped_files}")


# --- Add Menu Item to Browser --- 
def add_browser_menu_item(browser: Browser):
    action = QAction("Tone Viewer: Process Selected Notes' Audio...", browser)
    action.triggered.connect(lambda: process_single_note_audio_action(browser))
    browser.form.menuEdit.addSeparator()
    browser.form.menuEdit.addAction(action)

gui_hooks.browser_menus_did_init.append(add_browser_menu_item)

# --- Worker function for multiprocessing ---
def _process_single_file_worker(task_info: tuple, force_overwrite: bool) -> dict:
    """Processes a single MP3 file. Designed for multiprocessing pool.
    Args:
        task_info: Tuple containing (nid, field_name, mp3_filename, mp3_path, json_path)
        force_overwrite: Boolean indicating whether to overwrite existing JSON.

    Returns:
        Dictionary with status and message.
    """
    nid, field_name, mp3_filename, mp3_path, json_path = task_info
    status = {"status": "unknown", "filename": mp3_filename, "nid": nid}

    if not mp3_processor: # Check if processor module is loaded in the worker process
        status["status"] = "error"
        status["message"] = "MP3 Processor module not available in worker."
        return status

    if not os.path.exists(mp3_path):
        status["status"] = "skipped"
        status["message"] = "MP3 file not found"
        return status

    if os.path.exists(json_path) and not force_overwrite:
        status["status"] = "skipped"
        status["message"] = "JSON exists and overwrite=False"
        return status

    try:
        success = mp3_processor.process_mp3_to_json(mp3_path, json_path)
        if success:
            status["status"] = "success"
        else:
            status["status"] = "failed"
            status["message"] = "Processor returned False"
    except Exception as e:
        print(f"Worker Error processing {mp3_filename} for note {nid}: {e}", file=sys.stderr)
        status["status"] = "failed"
        status["message"] = str(e)

    return status

# --- Background task runner for bulk processing ---
def _run_bulk_processing(tasks: list, force_overwrite: bool):
    """Runs the bulk processing using a multiprocessing pool and updates progress."""
    num_tasks = len(tasks)
    if num_tasks == 0:
        tooltip("No audio files found to process for the selected criteria.")
        return

    # Determine number of workers (can be adjusted)
    # Use slightly fewer than total cores to leave resources for UI/OS
    # num_workers = max(1, os.cpu_count() - 1 if os.cpu_count() > 1 else 1)
    # --- NEW: Try using all available CPU cores --- 
    num_workers = max(1, os.cpu_count())
    print(f"ToneViewer Bulk: Starting processing for {num_tasks} files using {num_workers} workers.")

    processed_count = 0
    success_count = 0
    skipped_count = 0
    failed_count = 0

    # Create a partial function with force_overwrite baked in
    worker_func = functools.partial(_process_single_file_worker, force_overwrite=force_overwrite)

    # Start progress (removed immediate=True)
    mw.progress.start(label="Bulk Processing Audio...")

    try:
        with multiprocessing.Pool(processes=num_workers) as pool:
            # Use imap_unordered for better progress reporting as tasks finish
            results = pool.imap_unordered(worker_func, tasks)
            for result in results:
                processed_count += 1
                if result["status"] == "success":
                    success_count += 1
                elif result["status"] == "skipped":
                    skipped_count += 1
                else: # failed or error
                    failed_count += 1
                    # Optionally log more details about failures here if needed
                    # print(f"Bulk Processing Failed: {result}")

                # --- NEW: Update progress bar on main thread --- 
                current_progress = processed_count # Capture current value
                mw.taskman.run_on_main(
                    lambda: mw.progress.update(value=current_progress)
                )
                # ---------------------------------------------
                
                if mw.progress.want_cancel():
                    print("ToneViewer Bulk: Cancellation requested.")
                    pool.terminate() # Attempt to stop workers
                    pool.join() # Wait for termination
                    break
    except Exception as e:
         print(f"ToneViewer Bulk: Error during multiprocessing: {e}", file=sys.stderr)
         # Ensure progress finishes even if there's a pool error
         mw.taskman.run_on_main(mw.progress.finish)
         mw.taskman.run_on_main(lambda err=str(e): showWarning(f"Bulk processing error: {err}")) # Capture exception string
         return # Stop further execution
    finally:
        # Ensure progress is always finished, run on main thread
        mw.taskman.run_on_main(mw.progress.finish)

    # Show summary message on main thread
    summary_msg = f"Bulk processing complete.\nSuccess: {success_count}\nSkipped: {skipped_count}\nFailed: {failed_count}"
    # Perform replacement outside the f-string for compatibility
    log_summary = summary_msg.replace('\n', ' ')
    print(f"ToneViewer Bulk: {log_summary}")
    # --- NEW: Show tooltip on main thread ---
    mw.taskman.run_on_main(lambda msg=summary_msg: tooltip(msg, period=3000)) # Capture message
    # --------------------------------------


# --- Ensure unique setup for the Tools -> Tone Viewer menu ---
if not hasattr(mw, "toneViewerMenu"): # Create submenu if it doesn't exist
    mw.toneViewerMenu = mw.form.menuTools.addMenu("Tone Viewer")
    print("ToneViewer Addon: Created 'Tone Viewer' submenu.")

# Add the Bulk Processing action (ensure defined only once)
if 'bulk_process_action' not in globals():
    bulk_process_action = QAction("Bulk Process Audio...", mw)
    bulk_process_action.setToolTip(
        "Process audio files in selected fields for a chosen note type "
        "to generate pitch data (.json files) used by the Tone Viewer graph.\n\n"
        "Select the note type and the audio field(s) containing the relevant MP3s. "
        "You can optionally limit the number of notes processed for testing. "
        "The 'Force Overwrite' option will re-process files even if JSON already exists.\n\n"
        "Note: This can be very time-consuming and CPU-intensive for large numbers of notes, "
        "but it runs in the background so you can continue using Anki."
    )
    bulk_process_action.triggered.connect(show_bulk_process_dialog)
    mw.toneViewerMenu.addAction(bulk_process_action)
    print("ToneViewer Addon: Added 'Bulk Process Audio...' menu item.")

print("ToneViewer Addon: Final load check complete.") # Add a final message 