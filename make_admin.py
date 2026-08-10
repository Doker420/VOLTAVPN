"""
Helper script to grant or revoke admin rights for a VOLTA user.

Usage:
    python make_admin.py <username>            # grant admin
    python make_admin.py <username> --revoke   # revoke admin
    python make_admin.py --telegram <id>       # grant admin by telegram id
    python make_admin.py --list                # list current admins
"""
import sys
from app import create_app, db
from app.models import User


def main():
    args = sys.argv[1:]
    app = create_app()
    with app.app_context():
        if not args or args[0] == '--help':
            print(__doc__)
            return

        if args[0] == '--list':
            admins = User.query.filter_by(is_admin=True).all()
            if not admins:
                print("No admins found.")
            for u in admins:
                print(f"  #{u.id}  {u.username}  (telegram_id={u.telegram_id})")
            return

        revoke = '--revoke' in args
        value = not revoke

        if args[0] == '--telegram' and len(args) > 1:
            user = User.query.filter_by(telegram_id=int(args[1])).first()
        else:
            user = User.query.filter_by(username=args[0]).first()

        if not user:
            print("User not found.")
            return

        user.is_admin = value
        db.session.commit()
        state = "granted" if value else "revoked"
        print(f"Admin rights {state} for {user.username} (#{user.id}).")


if __name__ == '__main__':
    main()
