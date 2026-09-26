"""
Entrypoint for Render.com Free Web Service.
Usage:
    python -m cryptoarb.strategies.obi_lead_lag.main
"""

import os
import uvicorn

def run():
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting OBI Lead-Lag Web Service on 0.0.0.0:{port}...")
    uvicorn.run(
        "cryptoarb.strategies.obi_lead_lag.server:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,  # Reduce logging CPU/disk overhead on Render
    )

if __name__ == "__main__":
    run()
