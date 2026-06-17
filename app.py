import os
import random
import threading
import time

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'snek-secret-key-2024'
_async_mode = 'gevent' if os.environ.get('RENDER') else 'threading'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode=_async_mode)

GRID_W = 50
GRID_H = 40
TICK_RATE = 0.1  # 10 ticks/sec

COLORS = [
    '#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4',
    '#F7DC6F', '#DDA0DD', '#FF9F43', '#00D2D3',
    '#54A0FF', '#5F27CD', '#01CBC6', '#FF9FF3',
]

OPPOSITES = {'up': 'down', 'down': 'up', 'left': 'right', 'right': 'left'}
DIR_DELTA = {'up': (0, -1), 'down': (0, 1), 'left': (-1, 0), 'right': (1, 0)}

game = {
    'players': {},   # sid -> player dict
    'food': [],      # list of {x, y}
    'kills': {},     # sid -> kill count (persists across respawns)
}
lock = threading.Lock()
color_counter = 0


def next_color():
    global color_counter
    c = COLORS[color_counter % len(COLORS)]
    color_counter += 1
    return c


def find_empty_spawn():
    occupied = set()
    for p in game['players'].values():
        for seg in p['body']:
            occupied.add((seg['x'], seg['y']))
    for _ in range(200):
        x = random.randint(3, GRID_W - 4)
        y = random.randint(3, GRID_H - 4)
        if (x, y) not in occupied:
            return x, y
    return random.randint(3, GRID_W - 4), random.randint(3, GRID_H - 4)


def make_snake(sid, name, color):
    x, y = find_empty_spawn()
    direction = random.choice(['up', 'down', 'left', 'right'])
    dx, dy = DIR_DELTA[direction]
    body = [
        {'x': x, 'y': y},
        {'x': x - dx, 'y': y - dy},
        {'x': x - 2 * dx, 'y': y - 2 * dy},
    ]
    return {
        'id': sid,
        'name': name,
        'body': body,
        'direction': direction,
        'next_direction': direction,
        'color': color,
        'alive': True,
        'score': 0,
        'ready': False,  # doesn't move until first input
    }


def replenish_food(target=None):
    if target is None:
        target = max(8, len(game['players']) * 3)
    occupied = set()
    for p in game['players'].values():
        for seg in p['body']:
            occupied.add((seg['x'], seg['y']))
    for f in game['food']:
        occupied.add((f['x'], f['y']))
    attempts = 0
    while len(game['food']) < target and attempts < 500:
        attempts += 1
        x = random.randint(0, GRID_W - 1)
        y = random.randint(0, GRID_H - 1)
        if (x, y) not in occupied:
            game['food'].append({'x': x, 'y': y})
            occupied.add((x, y))


def drop_food_at(positions, max_drop=5):
    occupied = {(f['x'], f['y']) for f in game['food']}
    dropped = 0
    for pos in positions:
        if dropped >= max_drop:
            break
        key = (pos['x'], pos['y'])
        if key not in occupied:
            game['food'].append({'x': pos['x'], 'y': pos['y']})
            occupied.add(key)
            dropped += 1


def tick():
    with lock:
        players = game['players']
        food = game['food']

        if not players:
            return

        replenish_food()

        # Build collision map: cell -> list of (sid, is_head)
        # We process all heads simultaneously to detect head-on collisions
        food_set = {(f['x'], f['y']) for f in food}

        # Map of every occupied body cell (excluding tails that will move)
        # We'll build this after computing new heads
        alive = {sid: p for sid, p in players.items() if p['alive'] and p.get('ready', True)}

        # Compute new heads
        new_heads = {}
        for sid, p in alive.items():
            # Apply queued direction (no 180° reversal)
            nd = p['next_direction']
            if nd != OPPOSITES.get(p['direction']):
                p['direction'] = nd
            dx, dy = DIR_DELTA[p['direction']]
            head = p['body'][0]
            new_heads[sid] = {'x': head['x'] + dx, 'y': head['y'] + dy}

        # Body cells after tails move (tail will be removed unless eating)
        # For collision: a snake's body minus its tail (tail vacates this tick)
        body_cells = {}  # (x,y) -> set of sids that own it
        for sid, p in alive.items():
            # Check if this snake will eat (to decide whether tail stays)
            nh = new_heads.get(sid)
            eats = nh and (nh['x'], nh['y']) in food_set
            segments = p['body'] if eats else p['body'][:-1]
            for seg in segments:
                key = (seg['x'], seg['y'])
                body_cells.setdefault(key, set()).add(sid)

        # Determine deaths
        killed = {}  # sid -> killer_sid or None
        head_positions = {}  # (x,y) -> list of sids arriving there
        for sid, nh in new_heads.items():
            key = (nh['x'], nh['y'])
            head_positions.setdefault(key, []).append(sid)

        dead_this_tick = set()

        for sid, nh in new_heads.items():
            if sid in dead_this_tick:
                continue
            nx, ny = nh['x'], nh['y']

            # Wall collision
            if nx < 0 or nx >= GRID_W or ny < 0 or ny >= GRID_H:
                dead_this_tick.add(sid)
                killed[sid] = None
                continue

            # Body collision (hit someone's body)
            owners = body_cells.get((nx, ny), set())
            if owners:
                dead_this_tick.add(sid)
                # Killer is the owner if it's not self, pick first other
                other = (owners - {sid})
                killed[sid] = next(iter(other)) if other else None
                continue

        # Head-on collision: multiple snakes arriving at same cell
        for cell, arrivals in head_positions.items():
            if len(arrivals) > 1:
                # All colliding snakes die (or larger wins — here all die)
                for sid in arrivals:
                    if sid not in dead_this_tick:
                        dead_this_tick.add(sid)
                        killed[sid] = None

        # Apply movement and deaths
        new_food_set = set(food_set)
        removed_food = []

        for sid, p in alive.items():
            if sid in dead_this_tick:
                p['alive'] = False
                # Drop some food where the snake was
                drop_food_at(p['body'][1:], max_drop=min(10, len(p['body']) // 2))
                # Award kill
                killer = killed.get(sid)
                if killer and killer in players:
                    players[killer]['score'] += max(1, len(p['body']) // 3)
                    game['kills'][killer] = game['kills'].get(killer, 0) + 1
                continue

            nh = new_heads[sid]
            eats = (nh['x'], nh['y']) in new_food_set
            p['body'].insert(0, nh)
            if eats:
                p['score'] += 1
                new_food_set.discard((nh['x'], nh['y']))
                removed_food.append({'x': nh['x'], 'y': nh['y']})
            else:
                p['body'].pop()

        # Update food list
        game['food'] = [f for f in food if (f['x'], f['y']) in new_food_set]

        # Emit state
        state = {
            'players': [
                {
                    'id': p['id'],
                    'name': p['name'],
                    'body': p['body'],
                    'color': p['color'],
                    'alive': p['alive'],
                    'score': p['score'],
                    'kills': game['kills'].get(p['id'], 0),
                    'length': len(p['body']),
                }
                for p in players.values()
            ],
            'food': game['food'],
            'grid': {'w': GRID_W, 'h': GRID_H},
        }

    socketio.emit('game_state', state)

    # Notify players who just died this tick (only once)
    for sid in dead_this_tick:
        with lock:
            p = game['players'].get(sid)
            if p:
                socketio.emit('you_died', {
                    'score': p['score'],
                    'kills': game['kills'].get(sid, 0),
                    'length': len(p['body']),
                }, room=sid)


def game_loop():
    while True:
        time.sleep(TICK_RATE)
        try:
            tick()
        except Exception as e:
            print(f'Tick error: {e}')


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('connect')
def on_connect():
    emit('config', {'grid_w': GRID_W, 'grid_h': GRID_H})


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    with lock:
        game['players'].pop(sid, None)
        game['kills'].pop(sid, None)


@socketio.on('join')
def on_join(data):
    sid = request.sid
    name = str(data.get('name', 'Snake'))[:16].strip() or 'Snake'
    with lock:
        color = next_color()
        player = make_snake(sid, name, color)
        game['players'][sid] = player
        if not game['kills'].get(sid):
            game['kills'][sid] = 0
    emit('joined', {'id': sid, 'color': color})


@socketio.on('dir')
def on_dir(data):
    sid = request.sid
    d = data.get('d')
    if d not in DIR_DELTA:
        return
    with lock:
        p = game['players'].get(sid)
        if p and p['alive']:
            if d != OPPOSITES.get(p['direction']):
                p['next_direction'] = d
            p['ready'] = True


@socketio.on('ping_check')
def on_ping():
    emit('pong_check')


@socketio.on('respawn')
def on_respawn():
    sid = request.sid
    with lock:
        p = game['players'].get(sid)
        if p and not p['alive']:
            new = make_snake(sid, p['name'], p['color'])
            game['players'][sid] = new


def start_game_loop():
    # start_background_task works correctly with both threading and gevent modes
    socketio.start_background_task(game_loop)


if __name__ == '__main__':
    start_game_loop()
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
else:
    # Running under gunicorn — start the loop when the module is imported
    start_game_loop()
