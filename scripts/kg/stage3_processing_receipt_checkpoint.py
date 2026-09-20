#!/usr/bin/env python3
"""Filesystem-only immutable checkpoint for completed receipt terminals.

``--bridge-*`` admits one terminal created under an intervening authorized packet
when a safety-code revision requires a fresh packet before the next batch.  The
bridge is verified under its own packet and must have the same target, plan and
backup as both checkpoint endpoints; no terminal is replayed or rewritten.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
REPO=Path(__file__).resolve().parents[2]
for p in (str(REPO),str(REPO/'scripts')):
    if p not in sys.path: sys.path.insert(0,p)
from scripts.kg import stage3_processing_receipt_continue as C
from scripts.kg.stage2_artifacts import load_verified, write_immutable

def build(plan, packet, prior, prior_packet, terminal_dir, end, *, preflight_document,
          bridge_packet=None, bridge_terminal=None):
    prior_doc = load_verified(prior)
    offset = int(prior_doc['totals']['selected'])
    effective_packet = bridge_packet or packet
    C._prior_aggregate(prior, prior_packet=prior_packet, packet=effective_packet, plan=plan,
                       start_offset=offset)
    windows = [{**w, 'terminal_receipt_path': w['path']} for w in prior_doc['windows']]
    if (bridge_packet is None) != (bridge_terminal is None):
        raise C.ContinuationRefused('bridge packet and terminal must be supplied together')
    if bridge_packet is not None:
        selected = min(int(bridge_packet['batch_size']), len(plan['records']) - offset)
        terminal = C._prior_terminal(bridge_terminal, prior_packet=bridge_packet, packet=packet,
                                     plan=plan, selected=selected, offset=offset)
        windows.append(terminal)
        offset += selected
    records=list(plan['records'])
    while offset < end:
        selected=min(int(packet['batch_size']),len(records)-offset)
        terminal=C._existing(terminal_dir, packet=packet, plan=plan, offset=offset,
                             selected=selected, preflight_document=preflight_document)
        if terminal is None: raise C.ContinuationRefused(f'missing terminal at offset {offset}')
        windows.append(terminal); offset += selected
    if offset != end: raise C.ContinuationRefused('checkpoint end is not a terminal boundary')
    return C._aggregate(packet=packet, plan=plan, windows=windows, start_offset=0,
                        max_batches=len(windows), stopped=None,
                        preflight_document=preflight_document)

def main(argv=None):
    a=argparse.ArgumentParser(); a.add_argument('--plan',type=Path,required=True); a.add_argument('--apply',type=Path,required=True); a.add_argument('--prior-apply',type=Path,required=True); a.add_argument('--prior-aggregate',type=Path,required=True); a.add_argument('--terminal-dir',type=Path,required=True); a.add_argument('--end-offset',type=int,required=True); a.add_argument('--out',type=Path,required=True); a.add_argument('--preflight',type=Path,required=True); a.add_argument('--bridge-apply',type=Path); a.add_argument('--bridge-terminal',type=Path); x=a.parse_args(argv)
    value=build(load_verified(x.plan),load_verified(x.apply),x.prior_aggregate,load_verified(x.prior_apply),x.terminal_dir,x.end_offset,preflight_document=load_verified(x.preflight),bridge_packet=load_verified(x.bridge_apply) if x.bridge_apply else None,bridge_terminal=x.bridge_terminal)
    print(json.dumps({'digest':write_immutable(x.out,value),'checkpoint':str(x.out)},sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
