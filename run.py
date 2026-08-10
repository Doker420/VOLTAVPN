from app import app
import os

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    # use_reloader must stay False: the reloader spawns a second process which
    # would start a second Telegram poller (Conflict: terminated by other
    # getUpdates) and a duplicate hourly collector.
    app.run(host='0.0.0.0', port=port, debug=debug, use_reloader=False)
