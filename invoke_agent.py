import argparse
import os
import sys
import time
import pathlib
import httpx

API_DEFAULT    = "http://127.0.0.1:8001"
REG_TOKEN_PATH = pathlib.Path("/tmp/lkml_reg_token")

def wait_for_api(api: str, timeout: int = 30) -> None:
    #poll /health until the api is up or timeout is reached 
    print(f"[invoke] Waiting for API at {api} ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try: 
            r = httpx.get(f"{api}/health", timeout=2)
            if r.status_code == 200:
                print("[invoke] API is ready.")
                return
        except httpx.RequestError:
            pass
        time.sleep(1)
    print("[invoke] ERROR: API did not become ready in time.", file=sys.stderr)
    sys.exit(1)

# read the registration token provided by api.py on startup
def read_reg_token() -> str:
    if not REG_TOKEN_PATH.exists():
        print(f"[invoke] ERROR: {REG_TOKEN_PATH} not found. Is the API running?",
              file=sys.stderr)
        sys.exit(1)
    token = REG_TOKEN_PATH.read_text().strip()
    if not token: 
        print("[invoke] ERROR: Registration token file is empty.", file=sys.stderr)
        sys.exit(1)
    return token 


# do the actual registration and extracts the session token 
def register_cutoff(api: str, reg_token: str, cutoff: str) -> str:
    #call post/ register and return the session token
    r = httpx.post(
        f"{api}/register",
        params={"token": reg_token, "cutoff": cutoff},
        timeout=10,
    )
    if r.status_code == 403:
        print("[invoke] ERROR: Registration token was invalid or already used.",
              file=sys.stderr)
        sys.exit(1)
    r.raise_for_status()
    session_token = r.json()["session_token"]
    print(f"[invoke] Registered cutoff {cutoff}. Session token: {session_token}")
    return session_token

#launch claude with the session token in the environment 
def launch_claude(session_token: str) -> None:
    env = os.environ.copy()
    env["LKML_SESSION_TOKEN"] = session_token
    print("[invoke] Launching claude ...")
    os.execvpe("claude", ["claude"], env)

def main():
    parser = argparse.ArgumentParser(description="Register cutoff and launch claude.")
    parser.add_argument("--cutoff", required=True,
                        help="Cutoff date YYYY-MM-DD. Emails after this date are hidden.")
    parser.add_argument("--api", default=API_DEFAULT,
                        help=f"API base URL (default: {API_DEFAULT})")
    args = parser.parse_args()

    wait_for_api(args.api)
    reg_token     = read_reg_token()
    session_token = register_cutoff(args.api, reg_token, args.cutoff)
    launch_claude(session_token)


if __name__ == "__main__":
    main()