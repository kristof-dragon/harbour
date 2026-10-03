"""Local administrator recovery: python -m harbour.recovery --help."""
import argparse
import getpass
import time

from . import store


def main():
    parser = argparse.ArgumentParser(description='Recover local access using trusted console access to the data volume. Back up the data first.')
    parser.add_argument('--user', help='Existing administrator username')
    parser.add_argument('--disable-2fa', action='store_true')
    parser.add_argument('--reset-password', action='store_true')
    parser.add_argument('--unban', metavar='IP', help='Clear this exact IP ban and failed-attempt count')
    args = parser.parse_args()
    if not (store.DATA / 'harbour.db').exists():
        parser.error('No existing Harbour database found')
    if not (args.unban or args.disable_2fa or args.reset_password):
        parser.error('Choose a recovery action')
    user = None
    if args.disable_2fa or args.reset_password:
        user = store.one('SELECT id,name FROM users WHERE name=? AND role="admin"', (args.user,))
        if not user:
            parser.error('Specify an existing administrator with --user')
    password = None
    if args.reset_password:
        password = getpass.getpass('New password (at least 12 characters): ')
        if len(password) < 12 or password != getpass.getpass('Repeat new password: '):
            parser.error('Passwords must match and be at least 12 characters')
        password = store.password_hash(password)
    with store.db() as con:
        if args.unban:
            con.execute('DELETE FROM ip_bans WHERE address=?', (args.unban,))
            con.execute('DELETE FROM login_attempts WHERE address=?', (args.unban,))
        if user:
            if args.disable_2fa:
                con.execute('UPDATE users SET totp_secret=NULL,totp_last=-1 WHERE id=?', (user['id'],))
                con.execute('DELETE FROM recovery_codes WHERE user_id=?', (user['id'],))
                con.execute('DELETE FROM mfa_pending WHERE user_id=?', (user['id'],))
            if password:
                con.execute('UPDATE users SET password=? WHERE id=?', (password,user['id']))
            con.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        con.execute('INSERT INTO auth_log (username,address,created,outcome,detail) VALUES (?,?,?,?,?)',
                    (user['name'] if user else 'console', 'local-console', time.time(), 'console_recovery',
                     'Unban '+args.unban if args.unban else 'Administrator credentials recovered; sessions revoked'))
    print('Recovery complete. Sign in again through the web UI.')


if __name__ == '__main__':
    main()
