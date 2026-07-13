import logging

import uvicorn

from app.config import DASHBOARD_PORT
from app.dashboard import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=DASHBOARD_PORT)
