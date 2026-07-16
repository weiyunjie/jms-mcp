import uvicorn

from .config import settings
from .server import app


def main():
    uvicorn.run(host="0.0.0.0", port=settings.server_port, app=app)


if __name__ == "__main__":
    main()

