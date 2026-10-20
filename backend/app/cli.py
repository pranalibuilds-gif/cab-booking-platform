import argparse
import getpass
import sys

from sqlalchemy import select

from app.config import get_settings
from app.main import create_app
from app.models import User, UserRole
from app.security import hash_password


def create_admin() -> int:
    settings = get_settings()
    application = create_app(settings)
    email = input("Admin email: ").strip().lower()
    full_name = input("Admin name: ").strip()
    password = getpass.getpass("Admin password (12+ characters): ")
    if len(password) < 12:
        print("Password must contain at least 12 characters.", file=sys.stderr)
        return 1
    with application.state.session_factory() as db:
        if db.scalar(select(User.id).where(User.email == email)) is not None:
            print("That email is already registered.", file=sys.stderr)
            return 1
        db.add(
            User(
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
            )
        )
        db.commit()
    print("Administrator account created.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="cab-booking")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create-admin")
    args = parser.parse_args()
    if args.command == "create-admin":
        return create_admin()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
