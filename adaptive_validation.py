"""Offline chronological walk-forward on point-in-time replay frames.

Frames contain now, histories and ranking captured at that time (not today's
universe). Caller supplies candidate configs; only TRAIN selects parameters.
Never downloads data, synthesizes fills or accepts future candles.
"""
import json
import tempfile
from pathlib import Path
from adaptive_portfolio import PaperPortfolio


def evaluate(frames, config):
    with tempfile.TemporaryDirectory(prefix='quantlab-validation-') as directory:
        p = PaperPortfolio(Path(directory)/'paper.sqlite3',config)
        try:
            for f in frames:
                if f.get('rankingAsOf',f['now']) > f['now']: raise ValueError('Future ranking')
                p.process(f['histories'],f['ranking'],f['now'],context_ok=f.get('contextOk',True))
            return p.snapshot()['metrics']
        finally: p.db.close()


def walk_forward(frames,candidates,train_end,validation_end,oos_end):
    times = [f['now'] for f in frames]
    if times != sorted(set(times)): raise ValueError('Frames must be unique and chronological')
    if not times or not times[0] < train_end < validation_end < oos_end: raise ValueError('Invalid periods')
    parts = [[f for f in frames if lo <= f['now'] < hi] for lo,hi in
             ((times[0],train_end),(train_end,validation_end),(validation_end,oos_end))]
    if any(not p for p in parts): raise ValueError('Each period needs frames')
    trained = [(config,evaluate(parts[0],config)) for config in candidates]
    if not trained: raise ValueError('No candidates')
    eligible = [(c,r) for c,r in trained if r['expectancyNet'] is not None and r['maxDrawdown'] < c['MAX_DRAWDOWN']*100]
    if not eligible: return dict(status='INSUFFICIENT_TRAIN_EVIDENCE',liveReady=False)
    chosen,train = max(eligible,key=lambda pair:pair[1]['expectancyNet'])
    validation,oos = evaluate(parts[1],chosen),evaluate(parts[2],chosen)
    return dict(status='COMPLETE',config=chosen,train=train,validation=validation,outOfSample=oos,
                liveReady=False,notes='Fresh capital per split; no parameter reselection on validation/OOS. Repeat with advancing windows.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset',help='JSON with frames, candidates, trainEnd, validationEnd, oosEnd')
    args = parser.parse_args()
    data = json.loads(Path(args.dataset).read_text())
    print(json.dumps(walk_forward(data['frames'],data['candidates'],data['trainEnd'],data['validationEnd'],data['oosEnd']),indent=2,allow_nan=False))
