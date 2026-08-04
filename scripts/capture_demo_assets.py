#!/usr/bin/env python3
"""Regenerate README screenshots and the short Gate demo animation."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from gate.flight_recorder import GateDemoTUI
from gate.review_store import ReplayReviewDataSource

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"


async def capture(name: str, event_index: int, *, detail: bool = False) -> Path:
    source = ReplayReviewDataSource()
    source.step(event_index)
    app = GateDemoTUI(source)
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        if detail:
            await pilot.press("enter")
            await pilot.pause()
        filename = f"{name}.svg"
        app.save_screenshot(filename=filename, path=str(ASSETS))
        app.exit()
    return ASSETS / filename


async def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    dashboard = await capture("gate-demo-dashboard", 9)
    detail = await capture("gate-demo-detail", 12, detail=True)

    sips = shutil.which("sips")
    magick = shutil.which("magick")
    if sips is None or magick is None:
        print(f"SVG screenshots written to {ASSETS}; sips/ImageMagick not found, GIF not generated")
        return

    subprocess.run(
        [
            sips,
            "-s",
            "format",
            "png",
            str(dashboard),
            "--out",
            str(ASSETS / "gate-demo-dashboard.png"),
        ],
        check=True,
        timeout=30,
        capture_output=True,
    )
    subprocess.run(
        [sips, "-s", "format", "png", str(detail), "--out", str(ASSETS / "gate-demo-detail.png")],
        check=True,
        timeout=30,
        capture_output=True,
    )

    with tempfile.TemporaryDirectory(prefix="gate-demo-frames-") as frame_dir:
        frames: list[str] = []
        for frame_number, event_index in enumerate((1, 3, 5, 7, 9, 11, 12)):
            svg = await capture(f"frame-{frame_number:02d}", event_index)
            png = Path(frame_dir) / f"frame-{frame_number:02d}.png"
            subprocess.run(
                [sips, "-s", "format", "png", str(svg), "--out", str(png)],
                check=True,
                timeout=30,
                capture_output=True,
            )
            svg.unlink()
            frames.append(str(png))
        subprocess.run(
            [
                magick,
                "-delay",
                "85",
                *frames,
                "-delay",
                "180",
                frames[-1],
                "-loop",
                "0",
                "-layers",
                "Optimize",
                str(ASSETS / "gate-demo.gif"),
            ],
            check=True,
            timeout=60,
        )
    print(f"Demo assets written to {ASSETS}")


if __name__ == "__main__":
    asyncio.run(main())
