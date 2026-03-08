from flask import Flask, render_template, request, session, redirect, url_for, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime
import random, os, uuid, base64, mimetypes
from pathlib import Path

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cskchat_secret_2024')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', max_http_buffer_size=50*1024*1024)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Twilio ─────────────────────────────────────────────────────────────────────
try:
    from twilio.rest import Client as TwilioClient
    TWILIO_SID   = os.environ.get('TWILIO_SID', '')
    TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
    TWILIO_PHONE = os.environ.get('TWILIO_PHONE', '')
    twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID else None
except:
    twilio_client = None

# ── In-Memory DB ───────────────────────────────────────────────────────────────
users      = {}   # phone -> {name, phone, color, about, avatar_letter, dp}
messages   = {}   # chat_id -> [{from,to,text,time,seen,id,type,file_url,deleted,reactions,reply_to}]
groups     = {}   # group_id -> {name, members, admin, avatar_letter, color, created}
group_msgs = {}   # group_id -> [{from,text,time,id,type,file_url,deleted,reactions}]
otp_store  = {}   # phone -> otp
online     = {}   # phone -> sid
sid_map    = {}   # sid -> phone
typing_map = {}   # chat_id -> {phone: bool}

COLORS = ['#25D366','#00BCD4','#FF7043','#AB47BC','#42A5F5',
          '#66BB6A','#FFA726','#EC407A','#26C6DA','#8D6E63']

def chat_id(a, b): return '_'.join(sorted([a, b]))
def rnd_color(): return random.choice(COLORS)

def send_sms(phone, otp):
    if twilio_client:
        try:
            twilio_client.messages.create(
                body=f"CSK Chat verification code: {otp}\nDo not share this code.",
                from_=TWILIO_PHONE, to=phone)
            return True
        except Exception as e:
            print(f"Twilio error: {e}")
    return False

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if 'phone' in session and session['phone'] in users:
        return redirect(url_for('home'))
    return render_template('index.html')

@app.route('/send_otp', methods=['POST'])
def send_otp():
    d = request.get_json()
    phone = d.get('phone','').strip()
    if not phone or len(phone) < 10:
        return jsonify({'ok': False, 'msg': 'Invalid number'})
    if not phone.startswith('+'): phone = '+' + phone
    otp = str(random.randint(100000, 999999))
    otp_store[phone] = otp
    sent = send_sms(phone, otp)
    if sent:
        return jsonify({'ok': True, 'msg': 'OTP sent!'})
    return jsonify({'ok': True, 'msg': 'OTP sent!', 'dev_otp': otp})

@app.route('/verify_otp', methods=['POST'])
def verify_otp():
    d     = request.get_json()
    phone = d.get('phone','').strip()
    otp   = d.get('otp','').strip()
    name  = d.get('name','').strip()[:25]
    if not phone.startswith('+'): phone = '+' + phone
    stored = otp_store.get(phone)
    if not stored or stored != otp:
        return jsonify({'ok': False, 'msg': 'Wrong OTP!'})
    is_new = phone not in users
    if is_new and not name:
        return jsonify({'ok': True, 'need_name': True})
    del otp_store[phone]
    if is_new:
        users[phone] = {
            'name': name, 'phone': phone,
            'color': rnd_color(), 'about': 'Hey! I am using CSK Chat.',
            'avatar_letter': name[0].upper(), 'dp': None
        }
    session['phone'] = phone
    return jsonify({'ok': True, 'redirect': '/home'})

@app.route('/home')
def home():
    if 'phone' not in session or session['phone'] not in users:
        return redirect('/')
    me = users[session['phone']]
    my = session['phone']
    contacts = []
    for p, u in users.items():
        if p == my: continue
        cid  = chat_id(my, p)
        msgs = messages.get(cid, [])
        last = next((m for m in reversed(msgs) if not m.get('deleted')), None)
        unread = sum(1 for m in msgs if m['to']==my and not m['seen'] and not m.get('deleted'))
        last_text = ''
        if last:
            if last.get('type') == 'image': last_text = '📷 Photo'
            elif last.get('type') == 'video': last_text = '🎥 Video'
            elif last.get('type') == 'audio': last_text = '🎵 Audio'
            elif last.get('type') == 'file': last_text = '📎 File'
            else: last_text = last['text'][:45]
        contacts.append({**u,
            'last_msg':  last_text,
            'last_time': last['time'] if last else '',
            'unread':    unread,
            'online':    p in online
        })
    
    # Groups
    my_groups = []
    for gid, g in groups.items():
        if my in g['members']:
            gmsgs = group_msgs.get(gid, [])
            last_gm = next((m for m in reversed(gmsgs) if not m.get('deleted')), None)
            unread_g = sum(1 for m in gmsgs if m.get('from') != my and my not in m.get('read_by', []))
            my_groups.append({
                **g, 'id': gid,
                'last_msg': last_gm['text'][:45] if last_gm else '',
                'last_time': last_gm['time'] if last_gm else g.get('created',''),
                'unread': unread_g
            })
    
    contacts.sort(key=lambda x: x['last_time'], reverse=True)
    my_groups.sort(key=lambda x: x['last_time'], reverse=True)
    return render_template('home.html', me=me, contacts=contacts, all_users=list(users.values()), my_groups=my_groups)

@app.route('/chat/<phone>')
def chat(phone):
    if 'phone' not in session or session['phone'] not in users:
        return redirect('/')
    if phone not in users:
        return redirect('/home')
    me    = users[session['phone']]
    other = users[phone]
    cid   = chat_id(session['phone'], phone)
    msgs  = messages.get(cid, [])
    for m in msgs:
        if m['to'] == session['phone']: m['seen'] = True
    return render_template('conversation.html',
        me=me, other=other, messages=msgs, online=phone in online)

@app.route('/group/<gid>')
def group_chat(gid):
    if 'phone' not in session or session['phone'] not in users:
        return redirect('/')
    if gid not in groups or session['phone'] not in groups[gid]['members']:
        return redirect('/home')
    me = users[session['phone']]
    g  = groups[gid]
    gmsgs = group_msgs.get(gid, [])
    # Mark read
    for m in gmsgs:
        if 'read_by' not in m: m['read_by'] = []
        if session['phone'] not in m['read_by']:
            m['read_by'].append(session['phone'])
    members = [users[p] for p in g['members'] if p in users]
    return render_template('group.html', me=me, group=g, gid=gid, messages=gmsgs, members=members, online_set=list(online.keys()))

@app.route('/create_group', methods=['POST'])
def create_group():
    if 'phone' not in session: return jsonify({'ok': False})
    d = request.get_json()
    name = d.get('name','').strip()[:40]
    member_phones = d.get('members', [])
    if not name or len(member_phones) < 1:
        return jsonify({'ok': False, 'msg': 'Name and at least 1 member required'})
    gid = str(uuid.uuid4())[:8]
    all_members = list(set([session['phone']] + member_phones))
    groups[gid] = {
        'name': name,
        'members': all_members,
        'admin': session['phone'],
        'color': rnd_color(),
        'avatar_letter': name[0].upper(),
        'about': 'CSK Chat Group',
        'created': datetime.now().strftime('%H:%M')
    }
    group_msgs[gid] = []
    return jsonify({'ok': True, 'gid': gid})

@app.route('/profile')
def profile():
    if 'phone' not in session: return redirect('/')
    return render_template('profile.html', me=users[session['phone']])

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'phone' not in session: return redirect('/')
    d = request.get_json()
    p = session['phone']
    if 'name' in d and d['name'].strip():
        users[p]['name'] = d['name'].strip()[:25]
        users[p]['avatar_letter'] = users[p]['name'][0].upper()
    if 'about' in d:
        users[p]['about'] = d['about'].strip()[:100]
    if 'dp' in d:
        # base64 image
        dp_data = d['dp']
        if dp_data and dp_data.startswith('data:'):
            try:
                header, encoded = dp_data.split(',', 1)
                ext = 'jpg'
                if 'png' in header: ext = 'png'
                elif 'gif' in header: ext = 'gif'
                fname = f"dp_{p.replace('+','')}_{int(datetime.now().timestamp())}.{ext}"
                fpath = os.path.join(UPLOAD_FOLDER, fname)
                with open(fpath, 'wb') as f:
                    f.write(base64.b64decode(encoded))
                users[p]['dp'] = f'/static/uploads/{fname}'
            except Exception as e:
                print(f"DP upload error: {e}")
    return jsonify({'ok': True})

@app.route('/upload_media', methods=['POST'])
def upload_media():
    if 'phone' not in session: return jsonify({'ok': False})
    d = request.get_json()
    data_url = d.get('data', '')
    filename = d.get('filename', 'file')
    if not data_url or not data_url.startswith('data:'):
        return jsonify({'ok': False})
    try:
        header, encoded = data_url.split(',', 1)
        # Determine type
        mime = header.split(';')[0].replace('data:', '')
        ext = mimetypes.guess_extension(mime) or '.bin'
        ext = ext.lstrip('.')
        if ext == 'jpe': ext = 'jpg'
        fname = f"media_{uuid.uuid4().hex[:12]}.{ext}"
        fpath = os.path.join(UPLOAD_FOLDER, fname)
        with open(fpath, 'wb') as f:
            f.write(base64.b64decode(encoded))
        file_url = f'/static/uploads/{fname}'
        # Determine category
        if mime.startswith('image'): ftype = 'image'
        elif mime.startswith('video'): ftype = 'video'
        elif mime.startswith('audio'): ftype = 'audio'
        else: ftype = 'file'
        return jsonify({'ok': True, 'url': file_url, 'type': ftype, 'filename': filename, 'mime': mime})
    except Exception as e:
        print(f"Upload error: {e}")
        return jsonify({'ok': False, 'msg': str(e)})

@app.route('/delete_message', methods=['POST'])
def delete_message():
    if 'phone' not in session: return jsonify({'ok': False})
    d = request.get_json()
    msg_id = d.get('id')
    cid = d.get('chat_id')
    for_all = d.get('for_all', False)
    phone = session['phone']
    
    # Check DMs
    if cid and cid in messages:
        for m in messages[cid]:
            if m['id'] == msg_id and m['from'] == phone:
                if for_all:
                    m['deleted'] = True
                    m['text'] = ''
                    socketio.emit('msg_deleted', {'id': msg_id, 'chat_id': cid}, room=cid)
                else:
                    m['deleted_for'] = m.get('deleted_for', []) + [phone]
                return jsonify({'ok': True})
    
    # Check Groups
    for gid, gmsgs in group_msgs.items():
        for m in gmsgs:
            if m['id'] == msg_id and m['from'] == phone:
                if for_all:
                    m['deleted'] = True
                    m['text'] = ''
                    socketio.emit('msg_deleted', {'id': msg_id, 'group_id': gid}, room=f'group_{gid}')
                return jsonify({'ok': True})
    
    return jsonify({'ok': False})

@app.route('/react_message', methods=['POST'])
def react_message():
    if 'phone' not in session: return jsonify({'ok': False})
    d = request.get_json()
    msg_id = d.get('id')
    emoji = d.get('emoji')
    cid = d.get('chat_id')
    gid = d.get('group_id')
    phone = session['phone']
    
    if cid and cid in messages:
        for m in messages[cid]:
            if m['id'] == msg_id:
                if 'reactions' not in m: m['reactions'] = {}
                if emoji not in m['reactions']: m['reactions'][emoji] = []
                if phone in m['reactions'][emoji]:
                    m['reactions'][emoji].remove(phone)
                else:
                    m['reactions'][emoji].append(phone)
                if not m['reactions'][emoji]: del m['reactions'][emoji]
                socketio.emit('reaction', {'id': msg_id, 'reactions': m['reactions'], 'chat_id': cid}, room=cid)
                return jsonify({'ok': True})
    
    if gid and gid in group_msgs:
        for m in group_msgs[gid]:
            if m['id'] == msg_id:
                if 'reactions' not in m: m['reactions'] = {}
                if emoji not in m['reactions']: m['reactions'][emoji] = []
                if phone in m['reactions'][emoji]:
                    m['reactions'][emoji].remove(phone)
                else:
                    m['reactions'][emoji].append(phone)
                if not m['reactions'][emoji]: del m['reactions'][emoji]
                socketio.emit('reaction', {'id': msg_id, 'reactions': m['reactions'], 'group_id': gid}, room=f'group_{gid}')
                return jsonify({'ok': True})
    
    return jsonify({'ok': False})

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# ── SocketIO ───────────────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    if 'phone' not in session: return False
    p = session['phone']
    online[p] = request.sid
    sid_map[request.sid] = p
    emit('status', {'phone': p, 'online': True}, broadcast=True)

@socketio.on('disconnect')
def on_disconnect():
    p = sid_map.pop(request.sid, None)
    if p and p in online:
        del online[p]
        emit('status', {'phone': p, 'online': False}, broadcast=True)

@socketio.on('join')
def on_join(data):
    other = data.get('phone')
    if other: join_room(chat_id(session['phone'], other))

@socketio.on('join_group')
def on_join_group(data):
    gid = data.get('gid')
    if gid and gid in groups and session['phone'] in groups[gid]['members']:
        join_room(f'group_{gid}')

@socketio.on('message')
def on_message(data):
    if 'phone' not in session: return
    frm  = session['phone']
    to   = data.get('to','')
    text = data.get('text','').strip()[:2000]
    mtype = data.get('type','text')
    file_url = data.get('file_url','')
    filename = data.get('filename','')
    reply_to = data.get('reply_to', None)
    
    if not to or to not in users: return
    if mtype == 'text' and not text: return
    
    cid = chat_id(frm, to)
    msg = {
        'from': frm, 'to': to, 'text': text,
        'time': datetime.now().strftime('%H:%M'),
        'seen': False, 'deleted': False,
        'id': f"{cid}_{uuid.uuid4().hex[:8]}",
        'type': mtype,
        'file_url': file_url,
        'filename': filename,
        'reactions': {},
        'reply_to': reply_to
    }
    messages.setdefault(cid, []).append(msg)
    if len(messages[cid]) > 1000: messages[cid].pop(0)
    
    emit('message', {**msg,
        'from_name': users[frm]['name'],
        'from_color': users[frm]['color'],
        'from_dp': users[frm].get('dp')
    }, to=cid)
    
    if to in online:
        emit('notification', {
            'from': frm, 'name': users[frm]['name'],
            'color': users[frm]['color'],
            'dp': users[frm].get('dp'),
            'text': ('📷 Photo' if mtype=='image' else '🎥 Video' if mtype=='video' else '🎵 Audio' if mtype=='audio' else '📎 File' if mtype=='file' else text[:50]),
            'chat_type': 'dm', 'chat_id': frm
        }, to=online[to])

@socketio.on('group_message')
def on_group_message(data):
    if 'phone' not in session: return
    frm = session['phone']
    gid = data.get('gid','')
    text = data.get('text','').strip()[:2000]
    mtype = data.get('type','text')
    file_url = data.get('file_url','')
    filename = data.get('filename','')
    reply_to = data.get('reply_to', None)
    
    if not gid or gid not in groups: return
    if frm not in groups[gid]['members']: return
    if mtype == 'text' and not text: return
    
    msg = {
        'from': frm, 'text': text,
        'time': datetime.now().strftime('%H:%M'),
        'deleted': False, 'read_by': [frm],
        'id': f"g{gid}_{uuid.uuid4().hex[:8]}",
        'type': mtype,
        'file_url': file_url,
        'filename': filename,
        'reactions': {},
        'reply_to': reply_to
    }
    group_msgs.setdefault(gid, []).append(msg)
    if len(group_msgs[gid]) > 1000: group_msgs[gid].pop(0)
    
    emit('group_message', {**msg,
        'from_name': users[frm]['name'],
        'from_color': users[frm]['color'],
        'from_dp': users[frm].get('dp'),
        'gid': gid
    }, to=f'group_{gid}')
    
    # Notify members
    for member in groups[gid]['members']:
        if member != frm and member in online:
            emit('notification', {
                'from': frm, 'name': f"{users[frm]['name']} @ {groups[gid]['name']}",
                'color': users[frm]['color'],
                'dp': users[frm].get('dp'),
                'text': ('📷 Photo' if mtype=='image' else '🎥 Video' if mtype=='video' else text[:50]),
                'chat_type': 'group', 'chat_id': gid
            }, to=online[member])

@socketio.on('typing')
def on_typing(data):
    to = data.get('to','')
    if not to: return
    emit('typing', {'phone': session['phone'], 'typing': data.get('typing', False)},
         to=chat_id(session['phone'], to), include_self=False)

@socketio.on('group_typing')
def on_group_typing(data):
    gid = data.get('gid','')
    if not gid: return
    emit('group_typing', {
        'phone': session['phone'],
        'name': users[session['phone']]['name'],
        'typing': data.get('typing', False)
    }, to=f'group_{gid}', include_self=False)

@socketio.on('seen')
def on_seen(data):
    frm = data.get('from','')
    if not frm: return
    cid = chat_id(session['phone'], frm)
    for m in messages.get(cid, []):
        if m['to'] == session['phone']: m['seen'] = True
    emit('seen', {'by': session['phone']}, to=cid, include_self=False)

if __name__ == '__main__':
    import eventlet
    eventlet.monkey_patch()
    socketio.run(app, debug=True, host='0.0.0.0',
                 port=int(os.environ.get('PORT', 5000)))
