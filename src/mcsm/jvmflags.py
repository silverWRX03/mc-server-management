"""Aikar's flags: garbage-collection settings that avoid lag spikes on big heaps.

Minecraft servers make lots of short-lived objects; with a large heap, Java's default
collector can pause for long enough that the game stutters. Aikar's tuned G1 settings
(https://docs.papermc.io/paper/aikars-flags) keep pauses short. Above 12 GB they use larger
regions and a bigger young generation, as the guide says.
"""

from __future__ import annotations

SUGGEST_ABOVE_GB = 16  # offer them when a server gets more memory than this

COMMON = [
    "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200", "-XX:+UnlockExperimentalVMOptions",
    "-XX:+DisableExplicitGC", "-XX:+AlwaysPreTouch", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
    "-XX:G1MixedGCLiveThresholdPercent=90", "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1",
    "-Dusing.aikars.flags=https://mcflags.emc.gs", "-Daikars.new.flags=true",
]
UP_TO_12G = ["-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
             "-XX:G1ReservePercent=20", "-XX:InitiatingHeapOccupancyPercent=15"]
OVER_12G = ["-XX:G1NewSizePercent=40", "-XX:G1MaxNewSizePercent=50", "-XX:G1HeapRegionSize=16M",
            "-XX:G1ReservePercent=15", "-XX:InitiatingHeapOccupancyPercent=20"]


def aikar(heap_bytes: int | None) -> list[str]:
    big = heap_bytes is not None and heap_bytes > 12 * 1024 ** 3
    return COMMON[:4] + (OVER_12G if big else UP_TO_12G) + COMMON[4:]
