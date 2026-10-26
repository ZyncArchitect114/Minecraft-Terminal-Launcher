import os
import platform
import sys
import subprocess
import hashlib
import uuid
import json
import urllib.request
import urllib.parse
import shutil

import minecraft_launcher_lib
from pathlib import Path

# ---------- constants ----------
VER = "Minecraft Terminal Launcher 0.0.1"
PLATFORM = platform.system()

BASE_DIR = Path.cwd()
PROFILE_DIR = BASE_DIR / ".MCTL" / "profile"
PROFILE_FILE = PROFILE_DIR / "profile.txt"
REFRESH_TOKEN_FILE = PROFILE_DIR / "refresh_token.txt"
MC_DIR = BASE_DIR / ".minecraft"

# Microsoft OAuth2
MSA_CLIENT_ID = "70ae9c83-c28b-4e19-8464-0607d12d0926"
MSA_REDIRECT_URI = "http://localhost:8080/callback"

MSA_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBOX_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_AUTH_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

PROFILE_DIR.mkdir(parents=True, exist_ok=True)
MC_DIR.mkdir(parents=True, exist_ok=True)


# ---------- utils ----------
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def safe_int(prompt: str, default=None):
    try:
        return int(input(prompt))
    except (ValueError, EOFError):
        print("Invalid input.")
        return default


# ---------- microsoft auth ----------
def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _msa_token_from_refresh_token(refresh_token: str) -> dict:
    data = {
        "client_id": MSA_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": MSA_REDIRECT_URI,
    }
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        MSA_TOKEN_URL,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        token_resp = json.loads(resp.read().decode("utf-8"))

    if "access_token" not in token_resp:
        raise RuntimeError(f"MSA refresh failed: {token_resp}")
    return token_resp


def refresh_minecraft_token(refresh_token: str) -> dict:
    """用 refresh_token 重新走 微软 -> Xbox -> MC，返回与首次登录一致的结构。"""
    msa = _msa_token_from_refresh_token(refresh_token)
    new_refresh_token = msa.get("refresh_token", refresh_token)
    access_token = msa["access_token"]

    # 1) XBL
    xbl = _post_json(XBOX_AUTH_URL, {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={access_token}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    })
    xbl_token = xbl["Token"]
    user_hash = xbl["DisplayClaims"]["xui"][0]["uhs"]

    # 2) XSTS
    xsts = _post_json(XSTS_URL, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    })
    xsts_token = xsts["Token"]

    # 3) 登录 Minecraft
    mc_auth = _post_json(MC_AUTH_URL, {
        "identityToken": f"XBL3.0 x={user_hash};{xsts_token}",
    })
    mc_token = mc_auth["access_token"]

    # 4) 取 profile
    req = urllib.request.Request(
        MC_PROFILE_URL,
        headers={"Authorization": f"Bearer {mc_token}"},
    )
    with urllib.request.urlopen(req) as resp:
        profile = json.loads(resp.read().decode("utf-8"))

    return {
        "name": profile["name"],
        "id": profile["id"],
        "access_token": mc_token,
        "refresh_token": new_refresh_token,
    }


def do_refresh(profile: dict) -> dict:
    """微软账号每次启动前静默续期，失败不阻断。"""
    if profile.get("mode") != "msa":
        return profile
    if not REFRESH_TOKEN_FILE.exists():
        print("No refresh_token saved, skipping refresh.")
        return profile

    refresh_token = REFRESH_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not refresh_token:
        return profile

    print("Refreshing Microsoft token ...")
    try:
        info = refresh_minecraft_token(refresh_token)
    except Exception as exc:
        print(f"Token refresh failed ({exc}); using cached token.")
        return profile

    PROFILE_FILE.write_text(
        f"2\n{info['name']}\n{info['id']}\n{info['access_token']}\n",
        encoding="utf-8",
    )
    REFRESH_TOKEN_FILE.write_text(info["refresh_token"], encoding="utf-8")

    print(f"Refreshed. Logged in as {info['name']}.")
    return {
        "mode": "msa",
        "username": info["name"],
        "uuid": info["id"],
        "token": info["access_token"],
    }


# ---------- profile ----------
def load_profile():
    if not PROFILE_FILE.exists():
        raise FileNotFoundError("profile.txt not found")
    lines = [line.strip() for line in PROFILE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    acc_type = lines[0]
    if acc_type == "1":
        return {"mode": "offline", "username": lines[1], "uuid": lines[2], "token": "offline"}
    if acc_type == "2":
        return {"mode": "msa", "username": lines[1], "uuid": lines[2], "token": lines[3]}
    raise ValueError(f"Unknown account type: {acc_type}")


def save_offline_profile(username: str, offline_uuid: uuid.UUID):
    PROFILE_FILE.write_text(f"1\n{username}\n{offline_uuid}\n", encoding="utf-8")


# ---------- first-time login ----------
def first_time_login():
    print("No account found! Select login mode:")
    print(" 1) Cracked / Offline account")
    print(" 2) Microsoft account")
    mode = safe_int(": ", default=0)

    if mode == 1:
        username = input("Input player name: ").strip()
        if not username:
            print("Empty name, aborting.")
            sys.exit(1)

        digest = hashlib.md5(f"OfflinePlayer:{username}".encode()).digest()
        b = bytearray(digest)
        b[6] = (b[6] & 0x0F) | 0x30
        b[8] = (b[8] & 0x3F) | 0x80
        offline_uuid = uuid.UUID(bytes=bytes(b))

        print(f"Generated offline UUID: {offline_uuid}")
        save_offline_profile(username, offline_uuid)
        print("Profile saved.")

    elif mode == 2:
        login_url, _, code_verifier = minecraft_launcher_lib.microsoft_account.get_secure_login_data(
            MSA_CLIENT_ID, MSA_REDIRECT_URI
        )

        print("=" * 60)
        print("Open this URL and log in:")
        print(login_url)
        print("=" * 60)
        callback_url = input("Paste the blank-page callback URL: ").strip()

        try:
            auth_code = minecraft_launcher_lib.microsoft_account.get_auth_code_from_url(callback_url)
        except Exception as exc:
            print(f"Failed to parse callback URL: {exc}")
            sys.exit(1)

        info = minecraft_launcher_lib.microsoft_account.complete_login(
            MSA_CLIENT_ID, None, MSA_REDIRECT_URI, auth_code, code_verifier
        )

        PROFILE_FILE.write_text(
            f"2\n{info['name']}\n{info['id']}\n{info['access_token']}\n",
            encoding="utf-8",
        )
        REFRESH_TOKEN_FILE.write_text(info["refresh_token"], encoding="utf-8")

        print(f"Logged in as: {info['name']}")
        print(f"UUID          : {info['id']}")
        print("Refresh token saved.")
    else:
        print("Invalid choice.")
        sys.exit(1)


# ---------- ui helpers ----------
def show_banner():
    print("=" * 20, "Launcher info", "=" * 20)
    print(f"Version  : {VER}")
    print(f"Platform : {PLATFORM}")
    print("=" * 55)


def show_account(profile: dict):
    print("=" * 20, "Account info", "=" * 20)
    label = "Offline" if profile["mode"] == "offline" else "Microsoft"
    print(f"Account type : {label}")
    print(f"Player name  : {profile['username']}")
    print(f"Player UUID  : {profile['uuid']}")
    print("=" * 54)


def list_versions_by_type(allowed: set):
    try:
        all_versions = minecraft_launcher_lib.utils.get_version_list()
        filtered = [v for v in all_versions if v.get("type") in allowed]
        for v in filtered:
            print(f"  {v['id']}  ({v['type']})")
    except Exception as exc:
        print(f"Failed to fetch version list: {exc}")


# ---------- install / launch ----------
def install_menu():
    while True:
        print("\nSelect version type to list:")
        print(" 1) Release   2) Snapshot   3) Beta")
        print(" 4) Alpha     5) All        6) Install a specific version")
        print(" 7) Back")
        choice = safe_int(": ", default=0)

        if choice == 1:
            list_versions_by_type({"release"})
        elif choice == 2:
            list_versions_by_type({"snapshot"})
        elif choice == 3:
            list_versions_by_type({"old_beta"})
        elif choice == 4:
            list_versions_by_type({"old_alpha"})
        elif choice == 5:
            list_versions_by_type({"release", "snapshot", "old_beta", "old_alpha"})
        elif choice == 6:
            version = input("Version id to install: ").strip()
            if not version:
                continue
            try:
                print(f"Installing Minecraft {version} ...")
                minecraft_launcher_lib.install.install_minecraft_version(version, str(MC_DIR))
                print("Installation finished.")
            except Exception as exc:
                print(f"Installation failed: {exc}")
        elif choice == 7:
            return
        else:
            print("Invalid choice.")


def run_menu(profile: dict):
    print("\nInstalled versions:")
    try:
        installed = minecraft_launcher_lib.utils.get_installed_versions(str(MC_DIR))
        for v in installed:
            print(f"  {v['id']}  ({v.get('type', 'unknown')})")
    except Exception as exc:
        print(f"Could not list installed versions: {exc}")

    print("\n 1) Launch Minecraft\n 2) Back")
    if input(": ").strip() != "1":
        return

    version = input("Version to launch: ").strip()
    if not version:
        return

    try:
        print("Ensuring game files are ready...")
        minecraft_launcher_lib.install.install_minecraft_version(version, str(MC_DIR))

        cmd = minecraft_launcher_lib.command.get_minecraft_command(
            version, str(MC_DIR), {
                "username": profile["username"],
                "uuid": profile["uuid"],
                "token": profile["token"],
            }
        )
        print("Starting Minecraft...")
        subprocess.Popen(cmd, cwd=str(MC_DIR))
    except FileNotFoundError:
        print("Java was not found. Please install Java and make sure it is on PATH.")
    except Exception as exc:
        print(f"Failed to launch Minecraft: {exc}")

def uninstall_menu(profile: dict):
    print("\nInstalled versions:")
    try:
        installed = minecraft_launcher_lib.utils.get_installed_versions(str(MC_DIR))
        for v in installed:
            print(f"  {v['id']}  ({v.get('type', 'unknown')})")
    except Exception as exc:
        print(f"Could not list installed versions: {exc}")

    print("\n 1) Uninstall Minecraft\n 2) Back")
    if input(": ").strip() != "1":
        return

    version = input("Version to uninstall: ").strip()
    if not version:
        return

    try:
        print(f"Uninstalling {version}")
        shutil.rmtree(f".minecraft/versions/{version}")
    except FileNotFoundError:
        print(f"Can't find version: {version}")
    except Exception as exc:
        print(f"Failed to uninstall Minecraft: {exc}")

# ---------- main loop ----------
def main():
    clear_screen()

    if not PROFILE_FILE.exists():
        first_time_login()
        clear_screen()

    try:
        profile = load_profile()
    except Exception as exc:
        print(f"Cannot load profile ({exc}). Delete {PROFILE_FILE} and restart.")
        sys.exit(1)

    profile = do_refresh(profile)

    print("Login successful!\n")

    while True:
        clear_screen()
        show_banner()
        print()
        show_account(profile)
        print()
        print("=" * 20, "Tools", "=" * 20)
        print(" 1) Install Game")
        print(" 2) Run Minecraft")
        print(" 3) Uninstall Minecraft")
        print(" 4) Quit")
        print("=" * 47)

        choice = safe_int(": ", default=0)
        if choice == 1:
            install_menu()
        elif choice == 2:
            run_menu(profile)
        elif choice == 3:
            uninstall_menu(profile)
        elif choice == 4:
            print("Bye!")
            sys.exit(0)
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()
