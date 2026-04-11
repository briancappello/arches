# Laptop Power Management — ThinkPad P1 Gen 7

Target hardware: Lenovo ThinkPad P1 Gen 7 with Intel Core Ultra 7 165H
(Meteor Lake) and NVIDIA RTX 3000 Ada Generation.

## Basic Usage

### Power Profiles

Switch between profiles from the **KDE battery icon** in the system tray,
or from the command line:

```
tuned-adm profile p1-balanced
```

| Profile          | KDE Panel Name | Best For                                                                                                                     |
|------------------|----------------|------------------------------------------------------------------------------------------------------------------------------|
| `p1-powersave`   | Power Saver    | Max battery life. Turbo off, CPU biased to E-cores, firmware power limits minimized, WiFi power save on, USB autosuspend on. |
| `p1-balanced`    | Balanced       | Daily driver. Kernel-driven frequency scaling, turbo available for bursts, moderate firmware envelope, WiFi power save on.   |
| `p1-performance` | Performance    | Full send. CPU locked to max frequency, turbo on, firmware uncapped, all power savings off.                                  |

Your selected profile **persists across reboots**. The system boots into
whatever you last chose.

### GPU + Ollama

The NVIDIA GPU is managed **independently** from the power profiles. When
idle and nothing holds it open, it suspends to D3 via RTD3 and draws ~0W.

```
power gpu on       # Start Ollama + Open WebUI, GPU wakes automatically
power gpu off      # Stop both, GPU returns to D3 (~0W)
power gpu status   # Show GPU power state and service status
```

The GPU can be spun up in **any** power profile. You can stay in Power Saver
(CPU sipping power) while running an LLM on the GPU at full speed.

### Battery Charging

The battery is configured to charge to **80%** by default (75/80 thresholds)
to maximize battery lifespan. When you need a full charge before travel:

```
power charge full       # Set thresholds to 100%, auto-restores when done
power charge conserve   # Manually restore 75/80% thresholds
power charge status     # Show battery level, thresholds, monitor state
```

`power charge full` starts a background monitor that automatically restores
the 75/80% thresholds and sends a desktop notification when the battery
reaches 100%. No need to remember to reset it.

### Full Status Overview

```
power status            # Show profile, battery, and GPU state at a glance
```

---

## Architecture

```
KDE Panel (battery icon)
    |
    v
tuned-ppd            Maps PPD tiers to custom tuned profiles
    |
    v
tuned                Applies sysfs writes, sysctl, scripts per profile
    |
    +-- CPU          governor, EPP, turbo, platform profile
    +-- Disk         ALPM, readahead
    +-- Network      WiFi power save
    +-- USB          autosuspend
    +-- Display      panel power savings
    +-- Kernel       sysctl tunables

power                Unified CLI for GPU and battery management
    |
    +-- power gpu    Ollama + Open WebUI + NVIDIA RTD3
    +-- power charge Battery charge threshold control
```

---

## Tunable Knobs Reference

All profiles live in `/etc/tuned/profiles/p1-*/tuned.conf`. Edit them
directly, then reload with `sudo systemctl restart tuned.service`.

### CPU — `[cpu]` section

| Key                             | Description                                                                                                                        | Values                                                               | Sysfs Path                                                           |
|---------------------------------|------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------|----------------------------------------------------------------------|
| `governor`                      | CPU frequency scaling policy. `powersave` lets EPP drive decisions. `schedutil` is kernel-driven. `performance` locks to max freq. | `performance`, `powersave`, `schedutil\|powersave` (fallback syntax) | `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`              |
| `energy_perf_bias`              | Intel Energy Performance Bias hint (legacy). Lower = more power savings.                                                           | `performance`, `normal`, `powersave`, `power`                        | `/sys/devices/system/cpu/cpu*/cpufreq/energy_perf_bias`              |
| `energy_performance_preference` | Intel HWP Energy Performance Preference. The primary knob that controls how aggressively the CPU uses P-cores vs E-cores.          | `performance`, `balance_performance`, `balance_power`, `power`       | `/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference` |
| `boost`                         | Turbo Boost. 1 = on, 0 = off. Disabling this is the single biggest power saver after screen brightness.                            | `0`, `1`                                                             | `/sys/devices/system/cpu/intel_pstate/no_turbo` (inverted)           |
| `min_perf_pct`                  | Minimum P-state as percentage. 100 = always run at max. Only useful for performance profile.                                       | `0`–`100`                                                            | `/sys/devices/system/cpu/intel_pstate/min_perf_pct`                  |

**EPP is the most impactful CPU knob.** On Meteor Lake, `power` heavily
favors E-cores and keeps P-cores asleep. `performance` keeps P-cores
ready to fire immediately.

### Platform Profile — `[acpi]` section

| Key                | Description                                                                                        | Values                                 |
|--------------------|----------------------------------------------------------------------------------------------------|----------------------------------------|
| `platform_profile` | Firmware-level power envelope. Controls PL1/PL2 power limits and fan curves via ThinkPad firmware. | `low-power`, `balanced`, `performance` |

This is the ThinkPad-specific knob exposed by `thinkpad_acpi`. It
controls how much sustained power the CPU is allowed to draw (PL1) and
the short burst limit (PL2), as well as fan behavior. The actual wattage
values are set by Lenovo's firmware and cannot be overridden from
userspace.

Sysfs path: `/sys/firmware/acpi/platform_profile`

### Disk — `[disk]` and `[scsi_host]` sections

| Key | Section | Description | Values |
|---|---|---|---|
| `readahead` | `[disk]` | Readahead buffer in KiB. Larger = better sequential throughput, more memory use. | e.g. `>4096` (the `>` means "at least") |
| `alpm` | `[scsi_host]` | Aggressive Link Power Management for SATA/NVMe. Controls link power state between host and storage. | `max_performance`, `medium_power`, `med_power_with_dipm`, `min_power` |

`med_power_with_dipm` is the sweet spot for most use — it allows the
link to sleep but uses Device Initiated Power Management for faster wake.
`min_power` saves more but can add latency to disk access.

### Network — `[script]` section (script.sh)

| Knob            | How it's set                                                        | Description                                                                                                        |
|-----------------|---------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| WiFi power save | `enable_wifi_powersave` / `disable_wifi_powersave` in `script.sh`   | Allows the WiFi radio to sleep between beacons. Saves power but can add ~20ms latency to first packet after idle.  |
| USB autosuspend | `enable_usb_autosuspend` / `disable_usb_autosuspend` in `script.sh` | Suspends idle USB devices after timeout. Saves power but can cause issues with some peripherals (mice, keyboards). |

These are managed by helper functions from `/usr/lib/tuned/functions`
and called from the profile's `script.sh`. Edit the script directly to
change behavior.

### Display — `[video]` section

| Key                   | Description                                                                                                                                                        | Values                         |
|-----------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------|
| `panel_power_savings` | Intel display panel power savings (PSR/ALPM). Higher = more aggressive. May cause slight flickering on the internal eDP panel. Has no effect on external monitors. | `0` (off), `1`, `2`, `3` (max) |

### Kernel — `[sysctl]` section

| Key                              | Description                                                                                                            | Values                            |
|----------------------------------|------------------------------------------------------------------------------------------------------------------------|-----------------------------------|
| `vm.laptop_mode`                 | Delays disk writes to allow the drive to spin down / sleep longer.                                                     | `0` (off), `5` (on)               |
| `vm.dirty_writeback_centisecs`   | How often the kernel flushes dirty pages to disk, in centiseconds. Higher = fewer wakeups, more data at risk on crash. | `500` (default, 5s), `1500` (15s) |
| `kernel.nmi_watchdog`            | Hardware NMI watchdog. Disabling saves a small amount of power.                                                        | `0` (off), `1` (on)               |
| `vm.swappiness`                  | How aggressively the kernel swaps. Lower = prefer keeping pages in RAM.                                                | `0`–`100` (default `60`)          |
| `kernel.sched_autogroup_enabled` | Groups tasks by TTY session for better interactive responsiveness.                                                     | `0` (off), `1` (on)               |

### Audio — `[audio]` section

| Key       | Description                                                             | Values          |
|-----------|-------------------------------------------------------------------------|-----------------|
| `timeout` | Seconds before the audio codec enters power save. 0 = never power save. | `0`, `10`, etc. |

---

## Battery Conservation

The battery charge thresholds (75/80%) are stored in the ThinkPad's
Embedded Controller (EC) firmware. They persist across reboots without
any software needing to re-apply them.

**Why 80%?** Li-ion batteries degrade fastest when held at high voltage.
At 25°C, a battery stored at 100% charge loses ~20% capacity per year
vs only ~4% at 40% charge. The 80% ceiling (~4.05V/cell) is the
recommended sweet spot between usable capacity and longevity.

**Why 75% start threshold?** The 5% hysteresis (start charging at 75%,
stop at 80%) prevents constant micro-cycling when plugged in. The
battery sits idle between 75-80% instead of continuously topping off.

| Threshold            | Sysfs Path                                            | Default |
|----------------------|-------------------------------------------------------|---------|
| Start charging below | `/sys/class/power_supply/BAT0/charge_start_threshold` | 75%     |
| Stop charging at     | `/sys/class/power_supply/BAT0/charge_stop_threshold`  | 80%     |

---

## NVIDIA GPU Power States

The GPU has four possible states:

| State                 | Power Draw | Latency  | How                              |
|-----------------------|------------|----------|----------------------------------|
| D0 Active (computing) | up to 80W  | —        | Running CUDA workload            |
| D0 Idle (P8)          | ~2-3W      | instant  | GPU on but nothing running       |
| D3 (RTD3 suspended)   | ~0W        | ~1-2 sec | No processes hold `/dev/nvidia*` |
| Fully off             | 0W         | reboot   | `envycontrol -s integrated`      |

RTD3 is configured via:
- `/etc/modprobe.d/nvidia-power.conf` — sets `NVreg_DynamicPowerManagement=0x02` (fine-grained)
- `/etc/udev/rules.d/80-nvidia-rtd3.rules` — sets `power/control=auto` on the NVIDIA PCI device

### Checking GPU state

```
power gpu status                                                # quick overview
cat /sys/bus/pci/devices/0000:01:00.0/power/runtime_status      # suspended or active
nvidia-smi                                                      # full details (wakes GPU if suspended)
```

### What prevents D3

Any process holding `/dev/nvidia*` open prevents RTD3 suspend.
Common culprits:

- **Ollama** — holds GPU with model in VRAM. Managed by `power gpu`.
- **Open WebUI** — imports CUDA libraries. Managed by `power gpu`.
- **Chromium/Electron apps** — may probe the NVIDIA GPU on launch. Fix by
  setting `__NV_PRIME_RENDER_OFFLOAD=0 __GLX_VENDOR_LIBRARY_NAME=mesa` in
  the app's environment.

To find what's holding the GPU:
```
lsof /dev/nvidia*
fuser /dev/nvidia*
```

---

## File Locations

| File                                            | Purpose                                       |
|-------------------------------------------------|-----------------------------------------------|
| `/etc/tuned/profiles/p1-powersave/tuned.conf`   | Powersave profile config                      |
| `/etc/tuned/profiles/p1-powersave/script.sh`    | Powersave WiFi/USB script                     |
| `/etc/tuned/profiles/p1-balanced/tuned.conf`    | Balanced profile config                       |
| `/etc/tuned/profiles/p1-balanced/script.sh`     | Balanced WiFi script                          |
| `/etc/tuned/profiles/p1-performance/tuned.conf` | Performance profile config                    |
| `/etc/tuned/ppd.conf`                           | Maps KDE panel tiers to tuned profiles        |
| `/etc/modprobe.d/nvidia-power.conf`             | NVIDIA RTD3 driver parameter                  |
| `/etc/udev/rules.d/80-nvidia-rtd3.rules`        | NVIDIA PCI power control udev rules           |
| `/usr/local/bin/power`                          | Unified power management CLI                  |
| `/usr/local/lib/power/set-charge-thresholds`    | Privileged helper for charge threshold writes |
| `/etc/sudoers.d/power`                          | Passwordless sudo for power script helpers    |

Ansible role: `ansible/roles/power/`

---

## Quick Troubleshooting

**Profile didn't apply after switching:**
```
sudo systemctl restart tuned.service
tuned-adm active
tuned-adm verify
```

**KDE panel doesn't show power profiles:**
```
systemctl status tuned-ppd.service
# Must be active. If failed, check /etc/tuned/ppd.conf for errors.
```

**GPU won't enter D3:**
```
power gpu status        # check for warnings about holders
lsof /dev/nvidia*       # find the culprit process
```

**GPU wakes briefly then re-suspends:**
Normal. Some applications probe the GPU on launch, then release it. RTD3
suspends it again within a few seconds.

**`power charge full` monitor didn't restore thresholds:**
```
power charge status     # check if monitor is still running
power charge conserve   # manually restore 75/80%
```
