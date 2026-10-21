#!/usr/bin/python3
"""Synthetic MI peer for host tests; never connects to hardware."""
import os
from pathlib import Path
import sys
import time

Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))

def emit(text):
    # Split records across OS writes to exercise framing independently of packets.
    middle = len(text) // 2
    sys.stdout.write(text[:middle])
    sys.stdout.flush()
    sys.stdout.write(text[middle:] + '\n')
    sys.stdout.flush()

emit('(gdb)')
for line in sys.stdin:
    token, command = line.rstrip().split('-', 1)
    if command.startswith('target-select'):
        emit('*stopped,reason="signal-received",frame={addr="0x08000000",func="main"}')
        emit(token + '^connected')
    elif command == 'thread-info':
        emit(token + '^done,threads=[{id="1",state="stopped"}]')
    elif command == 'exec-next':
        emit(token + '^running')
        emit('*running,thread-id="all"')
        # Intentionally no stopped event; tests must terminate on deadline.
    elif command in ('exec-step', 'exec-step-instruction'):
        emit(token + '^running')
        emit('*running,thread-id="all"')
        time.sleep(0.02)
        emit('*stopped,reason="end-stepping-range",frame={func="tick",line="42"}')
    elif command == 'exec-continue':
        emit(token + '^running')
        emit('*running,thread-id="all"')
    elif command.startswith('exec-interrupt'):
        emit(token + '^done')
        emit('*stopped,reason="signal-received",frame={func="tick"}')
    elif command.startswith('break-insert'):
        emit(token + '^done,bkpt={number="1",func="main"}')
    elif command.startswith('stack-list-frames'):
        emit(token + '^done,stack=[frame={level="0",func="tick"},frame={level="1",func="main"}]')
    elif command.startswith('data-evaluate-expression'):
        emit(token + r'^error,msg="No symbol \"missing\" in current context."')
    else:
        emit(token + '^done')
