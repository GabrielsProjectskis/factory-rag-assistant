import os

import uvicorn

if __name__ == "__main__":
    # 127.0.0.1 by default: only this machine. Set HOST=0.0.0.0 to serve the internal network.
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
