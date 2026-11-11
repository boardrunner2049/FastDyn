#!/usr/bin/env python3
"""Render a saved FastDyn flight recording to a shareable browser-encoded video."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import socket
import tempfile
import time
from types import SimpleNamespace
from flight_recording import ROOT, read_recording


def export_video(recording, output, *, camera='overview', browser_path=None):
    # Optional rendering tools stay outside the firmware/runtime dependency set.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError('Video export needs Playwright: install it with python -m pip install playwright') from error
    from fastdyn_flight_view import FlightViewServer

    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f'output already exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    config = SimpleNamespace(APP_PREFIX='mavcesium/', SERVER_PORT=port, SERVER_INTERFACE='127.0.0.1')
    with tempfile.TemporaryDirectory(prefix='fastdyn-video-') as work:
        server = FlightViewServer(config, work)
        server.recording.mission = recording['mission']
        server.recording.history = recording['history']
        server.recording.telemetry = recording.get('telemetry', {})
        server.recording.saved = True
        server.start()
        try:
            with sync_playwright() as playwright:
                executable = browser_path or shutil.which('google-chrome') or shutil.which('chromium')
                browser = playwright.chromium.launch(
                    executable_path=executable, headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                try:
                    context = browser.new_context(viewport={'width': 1280, 'height': 720}, accept_downloads=True)
                    origin = f'http://127.0.0.1:{port}'
                    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/') else route.abort())
                    page = context.new_page()
                    errors, downloads = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('download', lambda download: downloads.append(download))
                    page.goto(origin + '/mavcesium/', wait_until='networkidle')
                    page.wait_for_function('window.flightReplay && window.flightViewer.scene.globe.tilesLoaded', timeout=30000)
                    page.evaluate('data => window.flightReplay.load(data)', recording)
                    page.evaluate('camera => window.flightReplay.camera(camera)', camera)
                    mime = page.evaluate('window.flightReplay.videoType()')
                    if not mime:
                        raise RuntimeError('The selected browser has no supported video encoder')
                    expected_suffix = '.mp4' if mime.startswith('video/mp4') else '.webm'
                    if output.suffix.lower() != expected_suffix:
                        raise RuntimeError(f'This browser exports {expected_suffix}; use that output extension or select Chrome for MP4')
                    duration = float(recording['history'][-1]['t'])
                    print(f'Rendering {duration:.1f} seconds of recorded flight to {output.name} ({camera})', flush=True)
                    page.evaluate('''() => { window.flightReplay.exportVideo().catch(error => { window.flightView.exportError = error.message; }); }''')
                    deadline = time.monotonic() + duration + max(60, duration * 0.5)
                    last_progress = -10
                    while not downloads:
                        if time.monotonic() > deadline:
                            raise RuntimeError('Video export timed out')
                        page.wait_for_timeout(500)
                        state = page.evaluate('({error:window.flightView.exportError,progress:window.flightView.exportProgress||0})')
                        if state['error']:
                            raise RuntimeError(state['error'])
                        if errors:
                            raise RuntimeError('; '.join(errors))
                        if state['progress'] >= last_progress + 10:
                            last_progress = state['progress']
                            print(f'Video export: {last_progress}%', flush=True)
                    temporary = Path(work) / ('export' + expected_suffix)
                    downloads[0].save_as(temporary)
                    if temporary.stat().st_size < 1024:
                        raise RuntimeError('Video export produced an empty or truncated file')
                    if expected_suffix == '.mp4':
                        with temporary.open('rb') as stream:
                            if stream.read(8)[4:8] != b'ftyp':
                                raise RuntimeError('Browser did not produce an MP4 container')
                    if output.exists():
                        raise FileExistsError(f'output appeared during export: {output}')
                    shutil.copyfile(temporary, output)
                    print(f'Saved {output} ({output.stat().st_size / 1024 / 1024:.1f} MiB)', flush=True)
                finally:
                    browser.close()
        finally:
            server.stop()
            if server.thread:
                server.thread.join(timeout=5)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', nargs='?', type=Path, default=ROOT / 'fastdyn_work/betaflight_flight.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'out/betaflight/replays' / f'betaflight-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.mp4')
    parser.add_argument('--camera', choices=('overview', 'top', 'follow'), default='overview')
    parser.add_argument('--browser', help='Chrome/Chromium executable; defaults to an installed Chrome or Playwright Chromium')
    args = parser.parse_args(argv)
    try:
        data = read_recording(args.recording)
        export_video(data, args.output, camera=args.camera, browser_path=args.browser)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f'Cannot export flight video: {error}\n')


if __name__ == '__main__':
    main()
