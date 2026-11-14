#!/usr/bin/env python3
"""Analyze hard cuts and visual shot layouts in an existing video.

Requires: ffmpeg, ffprobe, numpy, opencv-python, Pillow, tqdm.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from tqdm import tqdm


def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def probe(path):
    data = json.loads(run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)]).stdout)
    v = next(s for s in data['streams'] if s['codec_type'] == 'video')
    return float(v.get('duration') or data['format']['duration']), int(v['width']), int(v['height'])


def find_cuts(path, threshold, detect_width, duration):
    """Detect hard cuts via FFmpeg scene-score filter with live progress.

    Uses `-t {duration}` so FFmpeg stops at the analysis limit and
    `-progress pipe:1` on stdout for the progress bar.  FFmpeg stderr
    (showinfo lines) is redirected to a temp log file to avoid pipe
    deadlocks; the log is parsed after the process exits.  The video is
    scaled to 640px width before detection for speed.
    """
    detect_w = min(detect_width, 640)
    filt = f"scale={detect_w}:-2,select='gt(scene,{threshold})',showinfo"
    cmd = ['ffmpeg','-hide_banner','-nostdin','-t',f'{duration:.3f}','-i',str(path),
           '-vf',filt,'-an','-f','null','-','-progress','pipe:1']
    fd, log_path = tempfile.mkstemp(prefix='ffmpeg_stderr_', suffix='.log')
    os.close(fd)
    pbar = tqdm(desc='Cut detection', unit='s', total=int(duration),
                file=sys.stderr, leave=True)
    last = -1.0
    try:
        with open(log_path,'w',encoding='utf-8') as log:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=log, text=True, bufsize=1)
            try:
                for line in proc.stdout:
                    m = re.match(r'out_time_ms=(\d+)', line.strip())
                    if m:
                        t = int(m.group(1)) / 1_000_000
                        if t - last >= 1.0:
                            pbar.update(int(t) - int(last))
                            last = int(t)
                            pbar.set_postfix(time=f'{int(t)}s')
            finally:
                proc.wait()
    finally:
        stderr = Path(log_path).read_text(encoding='utf-8', errors='replace')
        try:
            Path(log_path).unlink()
        except OSError:
            pass
    if proc.returncode:
        raise RuntimeError(f'ffmpeg exited with code {proc.returncode}:\n{stderr[-2500:]}')
    times = []
    for line in stderr.splitlines():
        if 'showinfo' not in line or 'pts_time:' not in line:
            continue
        m = re.search(r'pts_time:\s*([0-9.]+)', line)
        if m:
            t = float(m.group(1))
            if not times or t - times[-1] > 0.12:
                times.append(t)
    pbar.close()
    return times


def extract_frame(path, time_, width):
    p = subprocess.run(['ffmpeg','-v','error','-nostdin','-ss',f'{time_:.3f}',
                        '-i',str(path),'-frames:v','1','-vf',f'scale={width}:-2',
                        '-f','image2pipe','-vcodec','mjpeg','-'],capture_output=True)
    if p.returncode or not p.stdout:
        return None
    return cv2.imdecode(np.frombuffer(p.stdout,dtype=np.uint8),cv2.IMREAD_COLOR)


def feature(frame):
    small = cv2.resize(frame,(192,108))
    hsv = cv2.cvtColor(small,cv2.COLOR_BGR2HSV)
    vec = []
    for y in range(0,108,36):
        for x in range(0,192,48):
            h = cv2.calcHist([hsv[y:y+36,x:x+48]],[0,1],None,[12,8],[0,180,0,256])
            h = cv2.normalize(h,None).flatten()
            vec.extend(h)
    v = np.asarray(vec,np.float32)
    return v / max(np.linalg.norm(v),1e-9)


def stamp(t):
    sec = int(t)
    return f'{sec//3600:02d}:{sec//60%60:02d}:{sec%60:02d}'


def contact_sheet(items, out, cols=5, thumb_width=240, rows_per_page=5):
    if not items:
        return 0
    cell_h = int(thumb_width*9/16)+30
    pages = (len(items) + cols*rows_per_page - 1) // (cols*rows_per_page)
    for page, start in enumerate(tqdm(range(0, len(items), cols*rows_per_page),
                                       total=pages, desc='Contact sheets', unit='pages',
                                       leave=False), 1):
        batch = items[start:start+cols*rows_per_page]
        sheet = Image.new('RGB',(cols*thumb_width,rows_per_page*cell_h),'#ffffff')
        d = ImageDraw.Draw(sheet)
        for k,(frame,label) in enumerate(batch):
            img = Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
            img.thumbnail((thumb_width-8,cell_h-35))
            x = (k%cols)*thumb_width+(thumb_width-img.width)//2
            y = (k//cols)*cell_h
            sheet.paste(img,(x,y))
            d.text(((k%cols)*thumb_width+5,y+cell_h-27),label,fill='#111111')
        sheet.save(out/f'page_{page:03d}.jpg',quality=88)
    return pages


def main():
    ap = argparse.ArgumentParser(description='Find hard cuts and group similar video layouts')
    ap.add_argument('video', type=Path)
    ap.add_argument('--out', type=Path, default=Path('shot_analysis'))
    ap.add_argument('--threshold', type=float, default=0.30,
                    help='FFmpeg scene threshold; lower finds more cuts (default .30)')
    ap.add_argument('--detect-width', type=int, default=320)
    ap.add_argument('--thumb-width', type=int, default=400)
    ap.add_argument('--similarity', type=float, default=0.92,
                    help='Cosine similarity to group layouts (default .92)')
    ap.add_argument('--max-representatives', type=int, default=120)
    ap.add_argument('--max-minutes', type=float, default=None,
                    help='Limit analysis to the first N minutes (testing)')
    args = ap.parse_args()
    if not args.video.is_file():
        ap.error(f'Video not found: {args.video}')
    args.out.mkdir(parents=True, exist_ok=True)

    t_start = time.monotonic()
    duration, width, height = probe(args.video)
    if args.max_minutes is not None:
        duration = min(duration, args.max_minutes * 60)
    print(f'Video: {duration/60:.1f} min, {width}x{height}', flush=True)
    if args.max_minutes is not None:
        print(f'(limited to first {args.max_minutes} min for testing)', flush=True)

    print('Detecting hard cuts (this may take several minutes)...', flush=True)
    cuts = [t for t in find_cuts(args.video, args.threshold, args.detect_width, duration)
            if 0.2 < t < duration - 0.2]
    print(f'Found {len(cuts)} cuts', flush=True)

    bounds = [0.0] + cuts + [duration]
    shots = []
    clusters = []
    all_items = []
    total_shots = len(bounds) - 1

    print(f'Processing {total_shots} shots...', flush=True)
    pbar = tqdm(total=total_shots, desc='Shots', unit='shots', leave=True)
    for i, (a, b) in enumerate(zip(bounds[:-1], bounds[1:])):
        mid = (a + b) / 2
        frame = extract_frame(args.video, mid, args.thumb_width)
        if frame is not None:
            f = feature(frame)
            scores = [float(np.dot(f, c['feature'])) for c in clusters]
            best = int(np.argmax(scores)) if scores else -1
            if best >= 0 and scores[best] >= args.similarity:
                group = best
                clusters[group]['count'] += 1
                clusters[group]['seconds'] += b - a
            else:
                group = len(clusters)
                clusters.append({'feature': f, 'count': 1, 'seconds': b - a,
                                 'frame': frame, 'time': mid})
            shots.append({'shot': len(shots)+1, 'start': round(a,3), 'end': round(b,3),
                          'duration': round(b-a,3), 'middle': round(mid,3), 'group': group+1})
            all_items.append((frame, f'#{len(shots)} {stamp(mid)} G{group+1}'))
        pbar.update(1)
        pbar.set_postfix(cuts=len(cuts), groups=len(clusters))
    pbar.close()

    print('Writing CSV files...', flush=True)
    with (args.out/'shots.csv').open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['shot','start','end','duration','middle','group'])
        w.writeheader(); w.writerows(shots)
    ranked = sorted(enumerate(clusters,1), key=lambda z: z[1]['seconds'], reverse=True)
    with (args.out/'groups.csv').open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f); w.writerow(['group','shots','total_seconds','example_time'])
        for gid, c in ranked:
            w.writerow([gid, c['count'], round(c['seconds'],2), round(c['time'],3)])

    reps = [(c['frame'], f'G{gid} {stamp(c["time"])} {c["count"]} shots')
            for gid, c in ranked[:args.max_representatives]]
    (args.out/'representative_groups').mkdir(exist_ok=True)
    (args.out/'all_shots').mkdir(exist_ok=True)
    n_rep_pages = contact_sheet(reps, args.out/'representative_groups')
    n_all_pages = contact_sheet(all_items, args.out/'all_shots')

    elapsed = time.monotonic() - t_start
    summary = {'video': str(args.video), 'duration_seconds': duration,
               'resolution': [width, height],
               'scene_threshold': args.threshold, 'similarity_threshold': args.similarity,
               'detected_cuts': len(cuts), 'analyzed_shots': len(shots),
               'visual_groups': len(clusters),
               'contact_sheet_pages': {'representative_groups': n_rep_pages,
                                        'all_shots': n_all_pages},
               'elapsed_seconds': round(elapsed, 1),
               'note': 'Groups are visual approximations, not speaker identities or verified shot types.'}
    (args.out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f'Done in {elapsed:.0f}s. Results: {args.out.resolve()}')


if __name__ == '__main__':
    main()
