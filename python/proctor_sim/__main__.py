"""Drive a scripted candidate against a running gateway.

    python -m proctor_sim --list
    python -m proctor_sim --scenario phone
    python -m proctor_sim --scenario look_away --tamper drop_events

Runs in real time by default so the gateway's absence-of-telemetry rules
behave as they would in production. `--fast` skips the sleeping, which is
fine for exercising content rules but will trip the clock-skew detector.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
import websockets

from .client import SimulatedClient, Tamper
from .scenarios import BEHAVIOURAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="proctor_sim")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--scenario", default="honest", choices=sorted(BEHAVIOURAL))
    parser.add_argument("--tamper", default="none", choices=[t.value for t in Tamper])
    parser.add_argument("--exam-id", default="demo-exam")
    parser.add_argument("--candidate-ref", default="demo-candidate")
    parser.add_argument("--fast", action="store_true", help="do not sleep between events")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    return parser


async def run(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=args.url, timeout=10.0) as http:
        response = await http.post(
            "/v1/sessions",
            json={"exam_id": args.exam_id, "candidate_ref": args.candidate_ref},
        )
        response.raise_for_status()
        enrolment = response.json()

    session_id = enrolment["session_id"]
    sim = SimulatedClient.from_enrolment(
        session_id, enrolment["session_key_b64"], tamper=Tamper(args.tamper)
    )
    script = BEHAVIOURAL[args.scenario][1]()

    ws_url = args.url.replace("http://", "ws://").replace("https://", "wss://")
    endpoint = f"{ws_url}{enrolment['telemetry_url']}"
    print(f"session {session_id}\nscenario {args.scenario} tamper={args.tamper}\n")

    sent = 0
    previous_ms = 0
    async with websockets.connect(endpoint) as ws:
        for t_ms, frame in sim.frames(script):
            if not args.fast:
                await asyncio.sleep(max(0, t_ms - previous_ms) / 1000)
            previous_ms = t_ms
            await ws.send(frame)
            sent += 1

    async with httpx.AsyncClient(base_url=args.url, timeout=10.0) as http:
        status = (await http.get(f"/v1/sessions/{session_id}")).json()

    print(f"sent {sent} frames")
    print(json.dumps(status, indent=2))
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.list:
        width = max(len(name) for name in BEHAVIOURAL)
        for name, (description, _) in sorted(BEHAVIOURAL.items()):
            print(f"  {name:<{width}}  {description}")
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
