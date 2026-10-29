# FireVAD Console Example (ESP32-P4)

This is the primary interactive testing environment for the FireRedVAD pipeline on the **Waveshare ESP32-P4-WIFI6** (Chip Rev v1.3).

This console (REPL) allows you to interactively load AI models, stream audio from the digital `INMP441` or analog `ES8311` microphones, record test `.wav` files, and tune the Automatic Gain Control (AGC) and Pre-VAD thresholds in real-time.

## ⚙️ Prerequisites
- ESP-IDF **v6.0+** (Required for proper ESP32-P4 multi-core routing)
- Waveshare ESP32-P4-WIFI6 Development Board
- SD Card formatted as FAT32
- Python 3.8+ (for pushing models via the custom UART protocol)

### Microphone Options

#### 🏆 INMP441 Digital I2S Microphone (Recommended)
- **Pins:** BCLK=20, WS=21, DIN=22
- **SNR:** **23.2 dB** (excellent)
- **Noise Floor:** -67.99 dBFS
- **Performance at 1m distance:** -44.73 dBFS
- **Advantages:**
  - Digital signal - immune to EMI/WiFi interference on PCB
  - Crystal clear, low-noise signal
  - Supports **stereo beamforming** (dual INMP441 wiring to same I2S bus)
  - No DC-offset or analog artifacts
- **Roadmap:** Easily expandable to stereo by wiring a 2nd INMP441 (flip L/R pin) for future beamforming and DoA detection

#### ES8311 Onboard Analog Microphone (Fallback)
- **Interface:** I2C (SDA=7, SCL=8) + I2S0 (MCLK=13, BCLK=12, WS=10, DIN=11, DOUT=9)
- **SNR:** 20.7 dB (at maximum 42 dB PGA gain)
- **Noise Floor:** -52.67 dBFS (requires aggressive gain boost)
- **Performance at 1m distance:** -31.91 dBFS (with 42 dB gain)
- **Limitations:**
  - Analog - highly susceptible to interference (LCD, MIPI, WiFi "tick tick" noise)
  - Requires hardware PGA gain boost (36-42 dB) for far-field VAD
  - **DC-offset "pop"** at startup (loud crack in first millisecond - handled by 80Hz HPF)
  - **Mono only** - physically limited to ONE ADC input (no beamforming possible)
  
**Verdict:** For production VAD, use the INMP441. The ES8311 is suitable for quick testing and as an environment sensor fallback.

## 1️⃣ Download and Transfer the AI Models

Instead of slowly flashing models via USB into a SPIFFS partition, this project uses a high-speed custom **UART File Protocol** to transfer models directly to your ESP32's SD card!

1. Download the `.frvd` models from HuggingFace to your PC:
```bash
pip install huggingface-hub pyserial
cd tools
python download_frvd_models.py
```
2. Build and flash the firmware (see step 2) and leave the ESP32 running.
3. Run the esp-file-bridge tool to push the models over Serial to the SD Card:
```bash
python -m pip install -e "<path-to-esp-uart-filebridge>\python"
esp-file-bridge upload_dir ../models_frvd /sd/models/ --port COM4
```

## 2️⃣ Build and Flash

```bash
idf.py set-target esp32p4
idf.py build flash monitor
```

> [!IMPORTANT]
> **Boot Calibration:** When the ESP32 boots, it performs a 1-second background noise calibration to configure the internal noise-gates. **You must remain completely silent when the message `PLEASE BE SILENT - CALIBRATING MICROPHONE...` appears on the screen.** After the calibration completes, the system will play a "System Ready" chime.

## 3️⃣ Using the Interactive Console

Once booted, you have access to a powerful CLI. Type `help` to see all commands.

### Quick Start: Live Microphone VAD

1. **Select the digital microphone:**
   ```bash
   mic_select inmp441
   ```
2. **Enable the Automatic Gain Control (AGC):**
   ```bash
   agc_enable 1
   ```
3. **Load the streaming (causal) INT8 model:**
   ```bash
   vad_model_load models/firered-stream-vad-int8.frvd
   ```
4. **Start the live VAD test for 30 seconds:**
   ```bash
   vad_infer_mic 30
   ```

### Full Command Reference

| Command | Description |
|---------|-------------|
| `mic_select <inmp441/es8311>` | Switches the active microphone source. |
| `mic_level` | Measures peak and RMS of the room for 1 second. |
| `agc_enable <0/1>` | Enables/Disables dynamic software gain (AGC). |
| `agc_info` | Shows current AGC RMS targets and gains. |
| `ls [path]` | Lists files and directories on the SD Card. |
| `vad_model_load <filename.frvd>` | Loads an INT8 or FP32 model from `/sd/<filename.frvd>` into PSRAM. |
| `vad_cascade_load <stream> <aed>` | Loads the Dual-Core Gatekeeper Cascade (Stream VAD + Offline AED). |
| `vad_infer_mic [sec]` | Starts live microphone inference for the specified duration. |
| `vad_infer_wav <path>` | Runs offline VAD inference on a test WAV file. |
| `vad_metrics` | Displays precise CPU cycle and microsecond latency timings. |
| `fs_ls [path]` | Lists files and directories on the SD card (`/sd/`). |
| `record_mic <file> <sec>` | Records live audio to a `.wav` file on the SD card for debugging. |
| `play_wav <file>` | Plays a `.wav` file through the ES8311 speaker output. |

## Filebridge Setup

# Filebridge Setup (Developer Example Only)

The developer example includes `esp-uart-filebridge` for transferring files between the device's SD card and your development PC over UART. This is a development tool, not part of the component's public API.

## Configuration

The filebridge runs on UART1 at 3,000,000 baud and mounts the SD card at `/sd`. Check the firmware startup output for actual port assignment and verify board wiring matches the configuration.

## Usage

### List Files

```bash
python -m esp_idf_uart_filebridge.cli --port COM_PORT --baud 3000000 ls /sd
```

### Download File

```bash
python -m esp_idf_uart_filebridge.cli --port COM_PORT --baud 3000000 download /sd/file.wav local/file.wav
```

Replace `COM_PORT` with your system's serial port connected to the filebridge UART.

## Requirements

- Python package: `esp-idf-uart-filebridge`
- Close any programs using the serial port during transfers
- The filebridge port is separate from the console port

## Notes

- File integrity: Compare downloaded file size with remote listing
- Port conflicts: Ensure exclusive access to the serial port during transfers
- Configuration details in example's `idf_component.yml` and README

## Filebridge Setup

# Filebridge Setup (Developer Example Only)

The developer example includes `esp-uart-filebridge` for transferring files between the device's SD card and your development PC over UART. This is a development tool, not part of the component's public API.

## Configuration

The filebridge runs on UART1 at 3,000,000 baud and mounts the SD card at `/sd`. Check the firmware startup output for actual port assignment and verify board wiring matches the configuration.

## Usage

### List Files

```bash
python -m esp_idf_uart_filebridge.cli --port COM_PORT --baud 3000000 ls /sd
```

### Download File

```bash
python -m esp_idf_uart_filebridge.cli --port COM_PORT --baud 3000000 download /sd/file.wav local/file.wav
```

Replace `COM_PORT` with your system's serial port connected to the filebridge UART.

## Requirements

- Python package: `esp-idf-uart-filebridge`
- Close any programs using the serial port during transfers
- The filebridge port is separate from the console port

## Notes

- File integrity: Compare downloaded file size with remote listing
- Port conflicts: Ensure exclusive access to the serial port during transfers
- Configuration details in example's `idf_component.yml` and README

