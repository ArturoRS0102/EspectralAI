import os
import random
import stripe
from openai import OpenAI
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from elevenlabs import ElevenLabs

# ==============================================================================
# CONFIGURACIÓN DE LA APLICACIÓN
# ==============================================================================

app = Flask(__name__)

# --- Configuración de Secretos ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'una-clave-secreta-muy-dificil-de-adivinar')

# --- Configuración de la Base de Datos ---
db_url = os.environ.get('DATABASE_URL', 'sqlite:///espectral.db')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


# --- Claves de API ---
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
ELEVENLABS_API_KEY = os.environ.get('ELEVENLABS_API_KEY')

# --- Inicialización de Servicios ---
db = SQLAlchemy(app)
migrate = Migrate(app, db)
openai_client = OpenAI(api_key=OPENAI_API_KEY)
stripe.api_key = STRIPE_SECRET_KEY

elevenlabs_client = None
if ELEVENLABS_API_KEY:
    elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

# ==============================================================================
# MODELOS DE BASE DE DATOS
# ==============================================================================

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    tokens = db.Column(db.Integer, default=10, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class GameSession(db.Model):
    __tablename__ = 'game_sessions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    game_mode = db.Column(db.String(50), nullable=False)
    history = db.Column(db.Text, default="")
    turn = db.Column(db.Integer, default=0)
    player_state = db.Column(db.JSON, default=lambda: {"inventory": [], "fear": 0})
    is_active = db.Column(db.Boolean, default=True)
    user = db.relationship('User', backref=db.backref('game_sessions', lazy=True))

# ==============================================================================
# LÓGICA DEL JUEGO Y VOZ
# ==============================================================================

GAME_MODES = {
    "casa_embrujada": {
        "title": "La Casa Embrujada",
        "description": "Explora una mansión abandonada donde los susurros en las paredes cuentan una historia macabra. ¿Resolverás el misterio o te convertirás en un eco más?",
        "initial_prompt": { "role": "system", "content": """Eres un narrador de terror gótico. El jugador es un investigador paranormal en la Mansión Blackwood. Inicia con una descripción CORTA y directa de la entrada. El objetivo es crear tensión inmediata. Describe la puerta principal y una ventana rota como opciones de entrada. Mantén las respuestas CONCISAS (máximo 150 palabras).""" }
    },
    "ouija_maldita": {
        "title": "La Ouija Maldita",
        "description": "Una noche de tormenta, tú y tus amigos decidís jugar a la Ouija. Lo que empieza como un juego, pronto se convierte en una lucha por vuestras almas.",
        "initial_prompt": { "role": "system", "content": """Eres el maestro de un juego de terror demoníaco. 5 amigos (jugador y 4 NPCs: Sara la escéptica, Leo el creyente, Ana la nerviosa, David el bromista) están en un sótano con una Ouija. Inicia la historia en el momento EXACTO en que ponen los dedos sobre la planchette. Describe la tensión y el primer movimiento antinatural de la planchette. Sé BREVE e impactante (máximo 150 palabras). Controlas a los NPCs y al demonio.""" }
    }
}

def generate_narrative_text(game_session, player_action=None):
    messages = []
    max_tokens = 450 
    if game_session.turn == 0:
        messages.append(GAME_MODES[game_session.game_mode]["initial_prompt"])
        messages.append({"role": "user", "content": "Comienza la narración. Describe la escena inicial de forma breve y directa."})
    else:
        system_prompt = f"""
        Eres un narrador de terror interactivo. Continúa la historia de forma CONCISA pero COMPLETA.
        - REGLA PRINCIPAL: ¡NUNCA termines una respuesta a media oración! Asegúrate de que cada respuesta sea un párrafo bien formado y concluido.
        - Modo de Juego: {GAME_MODES[game_session.game_mode]['title']}
        - Turno: {game_session.turn} de 20.
        - Objetivo: Generar un impacto inmediato. Las acciones deben tener consecuencias claras.
        - Regla de Acciones: Al final de tu narración, sugiere 2 o 3 acciones numeradas y audaces para el jugador. Ejemplo: **1. Examinar el libro.** **2. Salir corriendo.**
        - Historial Reciente (últimos 2 turnos): {game_session.history[-1000:]}
        """
        messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": f"Mi acción es: {player_action}"})

    try:
        response = openai_client.chat.completions.create(model="gpt-4o", messages=messages, max_tokens=max_tokens, temperature=0.9)
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"ERROR en generate_narrative_text: {e}")
        raise e

def generate_narrative_audio(text_to_speak):
    if not elevenlabs_client:
        print("ERROR: El cliente de ElevenLabs no está configurado.")
        return None
    
    cleaned_text = text_to_speak.replace('*', '')

    try:
        ADAM_VOICE_ID = "pNInz6obpgDQGcFmaJgB"
        
        # CORRECCIÓN: Se usa 'model_id' en lugar de 'model'
        audio_stream = elevenlabs_client.text_to_speech.stream(
            text=cleaned_text,
            voice_id=ADAM_VOICE_ID, 
            model_id="eleven_multilingual_v2"
        )
        return audio_stream
    except Exception as e:
        print(f"ERROR en generate_narrative_audio (ElevenLabs): {e}")
        return None
    
# ==============================================================================
# RUTAS DE LA APLICACIÓN
# ==============================================================================

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('menu'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('menu'))
    error = None
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']):
            session['user_id'] = user.id
            session['username'] = user.username
            session['user_tokens'] = user.tokens
            return redirect(url_for('menu'))
        else:
            error = "Usuario o contraseña incorrectos."
    return render_template('LOGIN_TEMPLATE.html', title="Iniciar Sesión", error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('menu'))
    error = None
    if request.method == 'POST':
        if User.query.filter_by(username=request.form['username']).first():
            error = "Ese nombre de usuario ya ha sido reclamado."
        else:
            new_user = User(username=request.form['username'], tokens=10)
            new_user.set_password(request.form['password'])
            db.session.add(new_user)
            db.session.commit()
            return redirect(url_for('login'))
    return render_template('REGISTER_TEMPLATE.html', title="Registro", error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/menu')
def menu():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user:
        session['user_tokens'] = user.tokens
    return render_template('MENU_TEMPLATE.html', title="Menú Principal", game_modes=GAME_MODES)

@app.route('/start_game', methods=['POST'])
def start_game():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    mode = request.form.get('mode')
    if mode not in GAME_MODES:
        return redirect(url_for('menu'))
    
    GameSession.query.filter_by(user_id=session['user_id'], is_active=True).update({"is_active": False})
    
    new_game = GameSession(user_id=session['user_id'], game_mode=mode, is_active=True)
    db.session.add(new_game)
    db.session.flush()

    initial_narrative = generate_narrative_text(new_game)
    new_game.history = initial_narrative
    new_game.turn = 1
    session['last_narrative_text'] = initial_narrative
    session['game_session_id'] = new_game.id
    
    db.session.commit()
    return redirect(url_for('play_game'))

@app.route('/play')
def play_game():
    if 'user_id' not in session or 'game_session_id' not in session:
        return redirect(url_for('menu'))
    game = db.session.get(GameSession, session.get('game_session_id'))
    if not game or not game.is_active:
        return redirect(url_for('menu'))
    game_title = GAME_MODES[game.game_mode]['title']
    return render_template('GAME_TEMPLATE.html', title=game_title, initial_narrative=game.history)

@app.route('/action', methods=['POST'])
def player_action():
    if 'user_id' not in session or 'game_session_id' not in session:
        return jsonify({'error': 'Sesión no válida'}), 401
    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return jsonify({'error': 'Tu sesión ha expirado.'}), 401
    if user.tokens <= 0:
        return jsonify({'error': 'No tienes suficientes tokens.', 'reason': 'no_tokens'}), 403
    game = db.session.get(GameSession, session['game_session_id'])
    if not game or not game.is_active:
        return jsonify({'error': 'La sesión de juego no está activa.'}), 400
    action = request.json.get('action')
    if not action:
        return jsonify({'error': 'Acción no proporcionada.'}), 400
    
    try:
        narrative = generate_narrative_text(game, action)
        user.tokens -= 1
        session['user_tokens'] = user.tokens
        full_action_text = f"\n\n>> Jugador: {action}\n\n{narrative}"
        game.history += full_action_text
        game.turn += 1
        session['last_narrative_text'] = narrative
        db.session.commit()
        return jsonify({'narrative': narrative, 'tokens': user.tokens})
    except Exception as e:
        print(f"API ERROR: {e}")
        return jsonify({'error': 'No se pudo generar la respuesta de la IA.'}), 500

@app.route('/narrate')
def narrate():
    text_to_speak = session.get('last_narrative_text', 'No hay nada que decir.')
    audio_stream = generate_narrative_audio(text_to_speak)
    
    if audio_stream:
        return Response(audio_stream, mimetype='audio/mpeg')
    
    return "Error al generar audio", 500

@app.route('/recharge')
def recharge_tokens():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('RECHARGE_TEMPLATE.html', title="Recargar Tokens")

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    if 'user_id' not in session:
        return jsonify(error="No autenticado"), 401
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{'price_data': {'currency': 'mxn', 'product_data': {'name': 'Paquete de 20 Tokens Espectrales'},'unit_amount': 5000},'quantity': 1}],
            mode='payment',
            success_url=url_for('menu', _external=True, payment='success'),
            cancel_url=url_for('menu', _external=True, payment='cancelled'),
            metadata={'user_id': session['user_id']}
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        return str(e)

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        return 'Webhook Error', 400
    if event['type'] == 'checkout.session.completed':
        checkout_session = event['data']['object']
        user_id = checkout_session.get('metadata', {}).get('user_id')
        if user_id:
            with app.app_context():
                user = db.session.get(User, int(user_id))
                if user:
                    user.tokens += 20
                    db.session.commit()
                    print(f"Tokens añadidos al usuario {user_id}")
    return 'Success', 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)