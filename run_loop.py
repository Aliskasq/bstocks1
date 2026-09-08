#!/usr/bin/env python3
"""Simple CLI entrypoint to run the agent loop."""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from app.agent.loop import AgentLoop

DEFAULT_GOAL = (
    "Find the best bStocks opportunity with moderate risk. "
    "Do not trade unless the setup meets all risk criteria."
)

def main():
    goal = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GOAL
    print(f"Starting agent loop with goal: {goal}")
    print("Press Ctrl+C to stop\n")

    loop = AgentLoop(goal)
    loop.start()

    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopping...")
        loop.stop()
        print(f"Cycles completed: {loop.cycles}")

if __name__ == "__main__":
    main()