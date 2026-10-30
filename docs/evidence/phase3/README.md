# Android host boot evidence

None of the archived attempts passes the Android framework gate. No Android iPhone
or APK execution is represented by these files.

- `h1` reaches second-stage init, then requests reboot after duplicate camera
  APEX selection aborts bootstrap. QEMU wall time is 59.971495 seconds and peak
  host RSS is 222052352 bytes.
- `h2` fixes that selection, sets up userdata, and launches zygote and
  SurfaceFlinger. It exceeds the 600-second deadline without verified framework
  completion. Wall time is 600.187618 seconds and peak host RSS is 245514240 bytes.
- `h3` runs the corrected real guest probe and reaches a system_server Binder
  caller. It waits for the repeatedly crashing stock light HAL. The run reaches
  its 1800-second deadline, with 312180736 bytes peak host RSS. The light HAL's
  unconfigured privileged vsock bind is the next configuration correction.
- `h4` supplies the light-service port and progresses beyond its failing bind,
  but still times out after 1800.085110 seconds without ActivityManager. Peak
  host RSS is 352960512 bytes and CPU utilization is 98.499% of one core.
- `phase1-host.log` records 62 passing checks from the existing host interpreter
  binary. `phase1-device.log` records the unchanged installed app's 6/6 gate on
  the physical iPhone17. The device console's signal15 is requested collector
  cleanup after the summary, not memory-pressure evidence.

The `.gz` files preserve complete original log bytes. `raw-log-sha256.json`
records their uncompressed digests. Commands, exact boot properties, original
results, submitted guest scripts, and console cleanup results accompany them.
No downloaded image binary is included.

`stage-report.json` uses the current parser. The original h2 result incorrectly
classified a zygote property-setting action at guest time 107.7 seconds as
execution. The current parser records the real service launch at 281.741958
seconds instead. Both the original output and corrected analysis are retained.
This correction does not change the failed framework verdict.

Reanalyse the compressed serial evidence without changing the original result:

```sh
PYTHONPYCACHEPREFIX="$PWD/build/android/pycache" python3 tools/android/report.py docs/evidence/phase3/h2
```

Stage times are guest kernel uptime printed in serial output, not host boot
latency. RSS and CPU come from the exact child process's wait4 measurements.
All four completed attempts are archived as failures. No guest is currently
running while further diagnostic capture is prepared.
