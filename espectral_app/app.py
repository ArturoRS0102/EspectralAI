import os
import random
import stripe
from openai import OpenAI
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
# --- Importación corregida para la v2.x de ElevenLabs ---
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

# --- Inicialización corregida del cliente de ElevenLabs ---
elevenlabs_client = None
if ELEVENLABS_API_KEY:
    # El nombre de la clase ahora es solo ElevenLabs
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
        # La llamada al método generate es a través del cliente
        audio_stream = elevenlabs_client.generate(
            text=cleaned_text,
            voice="Adam", 
            model="eleven_multilingual_v2",
            stream=True
        )
        return audio_stream
    except Exception as e:
        print(f"ERROR en generate_narrative_audio (ElevenLabs): {e}")
        return None

# ==============================================================================
# PLANTILLAS HTML
# ==============================================================================

BASE_TEMPLATE = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{{ title }} - Espectral</title><script src="https://cdn.tailwindcss.com"></script><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Creepster&family=Roboto:wght@300;400;700&display=swap" rel="stylesheet"><style>body { font-family: 'Roboto', sans-serif; } .font-creepster { font-family: 'Creepster', cursive; } .text-shadow { text-shadow: 2px 2px 4px rgba(0,0,0,0.7); } .story-text::first-letter { font-size: 1.5em; }</style></head><body class="bg-gray-900 text-gray-200 min-h-screen flex flex-col"><nav class="bg-black bg-opacity-50 shadow-lg"><div class="container mx-auto px-6 py-3 flex justify-between items-center"><a href="{{ url_for('menu') }}" class="text-3xl font-creepster text-red-500 tracking-wider text-shadow">ESPECTRAL</a><div>{% if 'user_id' in session %}<span class="mr-4">Tokens: <span id="token-count" class="font-bold text-yellow-400">{{ session.get('user_tokens', 0) }}</span></span><a href="{{ url_for('logout') }}" class="bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded-lg transition duration-300">Salir</a>{% endif %}</div></div></nav><main class="flex-grow container mx-auto p-4 md:p-6"></main><footer class="text-center text-gray-500 text-sm p-4">&copy; 2025 Espectral Interactive. Todos los derechos reservados.</footer></body></html>"""
LOGIN_CONTENT = """<div class="flex items-center justify-center h-full"><div class="w-full max-w-md bg-gray-800 bg-opacity-70 rounded-lg shadow-2xl p-8 border border-gray-700"><h2 class="text-4xl font-creepster text-center text-red-500 mb-6 text-shadow">Iniciar Sesión</h2>{% if error %}<div class="bg-red-900 border border-red-600 text-red-100 px-4 py-3 rounded-lg relative mb-4" role="alert"><span class="block sm:inline">{{ error }}</span></div>{% endif %}<form method="POST" action="{{ url_for('login') }}"><div class="mb-4"><label for="username" class="block text-gray-400 text-sm font-bold mb-2">Usuario</label><input type="text" name="username" id="username" class="shadow appearance-none border border-gray-600 rounded-lg w-full py-3 px-4 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:ring-2 focus:ring-red-500" required></div><div class="mb-6"><label for="password" class="block text-gray-400 text-sm font-bold mb-2">Contraseña</label><input type="password" name="password" id="password" class="shadow appearance-none border border-gray-600 rounded-lg w-full py-3 px-4 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:ring-2 focus:ring-red-500" required></div><div class="flex items-center justify-between"><button type="submit" class="w-full bg-red-600 hover:bg-red-700 text-white font-bold py-3 px-4 rounded-lg focus:outline-none focus:shadow-outline transition duration-300">Entrar en la Oscuridad</button></div></form><p class="text-center text-gray-500 text-sm mt-6">¿No tienes cuenta? <a href="{{ url_for('register') }}" class="font-bold text-red-500 hover:text-red-400">Regístrate aquí</a></p></div></div>"""
REGISTER_CONTENT = """<div class="flex items-center justify-center h-full"><div class="w-full max-w-md bg-gray-800 bg-opacity-70 rounded-lg shadow-2xl p-8 border border-gray-700"><h2 class="text-4xl font-creepster text-center text-red-500 mb-6 text-shadow">Crear Cuenta</h2>{% if error %}<div class="bg-red-900 border border-red-600 text-red-100 px-4 py-3 rounded-lg relative mb-4" role="alert"><span class="block sm:inline">{{ error }}</span></div>{% endif %}<form method="POST" action="{{ url_for('register') }}"><div class="mb-4"><label for="username" class="block text-gray-400 text-sm font-bold mb-2">Usuario</label><input type="text" name="username" id="username" class="shadow appearance-none border border-gray-600 rounded-lg w-full py-3 px-4 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:ring-2 focus:ring-red-500" required></div><div class="mb-6"><label for="password" class="block text-gray-400 text-sm font-bold mb-2">Contraseña</label><input type="password" name="password" id="password" class="shadow appearance-none border border-gray-600 rounded-lg w-full py-3 px-4 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:ring-2 focus:ring-red-500" required></div><div class="flex items-center justify-between"><button type="submit" class="w-full bg-red-600 hover:bg-red-700 text-white font-bold py-3 px-4 rounded-lg focus:outline-none focus:shadow-outline transition duration-300">Pactar con la Sombra</button></div></form><p class="text-center text-gray-500 text-sm mt-6">¿Ya tienes una cuenta? <a href="{{ url_for('login') }}" class="font-bold text-red-500 hover:text-red-400">Inicia sesión</a></p></div></div>"""
MENU_CONTENT = """<div class="text-center"><h1 class="text-5xl font-creepster text-red-500 mb-4 text-shadow">Elige tu Pesadilla</h1><p class="text-lg text-gray-400 mb-10">Cada historia es un nuevo descenso a la locura. Elige con cuidado.</p><div class="grid md:grid-cols-2 gap-8 max-w-4xl mx-auto">{% for mode_id, mode_data in game_modes.items() %}<div class="bg-gray-800 border border-gray-700 rounded-lg p-6 text-center hover:border-red-500 hover:shadow-2xl transition-all duration-300 transform hover:-translate-y-1"><h2 class="text-3xl font-creepster text-red-400 mb-3">{{ mode_data.title }}</h2><p class="text-gray-400 mb-6">{{ mode_data.description }}</p><form action="{{ url_for('start_game') }}" method="POST"><input type="hidden" name="mode" value="{{ mode_id }}"><button type="submit" class="bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-6 rounded-lg transition duration-300">Jugar</button></form></div>{% endfor %}</div><div class="mt-16"><h2 class="text-3xl font-creepster text-yellow-400 mb-4">Comprar Tokens</h2><p class="text-gray-400 mb-6">Necesitas más poder para enfrentarte a la oscuridad. Cada acción tiene un coste.</p><a href="{{ url_for('recharge_tokens') }}" class="bg-yellow-500 hover:bg-yellow-600 text-gray-900 font-bold py-3 px-8 rounded-lg transition duration-300">Recargar 20 Tokens por $50 MXN</a></div></div>"""
RECHARGE_CONTENT = """<div class="max-w-md mx-auto text-center bg-gray-800 p-8 rounded-lg border border-gray-700"><h2 class="text-4xl font-creepster text-yellow-400 mb-4">Recargar Energía</h2><p class="text-gray-400 mb-8">Adquiere 20 tokens para continuar tu aventura. La oscuridad no espera.</p><form action="{{ url_for('create_checkout_session') }}" method="POST"><button type="submit" class="w-full bg-yellow-500 hover:bg-yellow-600 text-gray-900 font-bold py-4 px-4 rounded-lg focus:outline-none focus:shadow-outline transition duration-300 text-lg">Pagar $50.00 MXN con Stripe</button></form></div>"""

GAME_CONTENT = """
<div class="max-w-4xl mx-auto bg-gray-800 bg-opacity-50 border border-gray-700 rounded-lg shadow-2xl p-4 sm:p-6 md:p-8">
    <div id="tts-controls" class="flex items-center justify-center gap-4 mb-4 p-2 bg-black bg-opacity-20 rounded-lg">
        <button id="play-btn" class="p-2 rounded-full bg-red-600 hover:bg-red-700 text-white" title="Reproducir/Reanudar"><svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" /><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg></button>
        <button id="pause-btn" class="p-2 rounded-full bg-gray-600 hover:bg-gray-700 text-white" title="Pausar"><svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg></button>
        <button id="stop-btn" class="p-2 rounded-full bg-gray-600 hover:bg-gray-700 text-white" title="Detener"><svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10h6v4H9z" /></svg></button>
    </div>
    <audio id="narrator-audio-player" class="hidden"></audio>
    <div id="story-container" class="mb-6 h-96 overflow-y-auto p-4 bg-black bg-opacity-20 rounded-lg border border-gray-700"><div id="narrative-content" class="text-lg text-gray-300 leading-relaxed whitespace-pre-wrap story-text">{{ initial_narrative | safe }}</div><div id="loading-indicator" class="hidden text-center p-4"><p class="text-red-500 animate-pulse">El más allá está respondiendo...</p></div></div>
    <form id="action-form" class="mt-4"><div class="mb-4"><label for="action_input" class="block text-gray-400 text-sm font-bold mb-2">¿Qué haces ahora?</label><textarea name="action_input" id="action_input" rows="2" class="shadow appearance-none border border-gray-600 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:ring-2 focus:ring-red-500" placeholder="Escribe tu acción aquí..."></textarea></div><div class="flex flex-col sm:flex-row gap-4"><button type="submit" id="submit-action" class="w-full sm:w-auto flex-grow bg-red-600 hover:bg-red-700 text-white font-bold py-3 px-4 rounded-lg focus:outline-none focus:shadow-outline transition duration-300">Actuar</button><a href="{{ url_for('menu') }}" class="w-full sm:w-auto text-center bg-gray-600 hover:bg-gray-700 text-white font-bold py-3 px-4 rounded-lg transition duration-300">Huir (Volver al Menú)</a></div></form>
    <div id="no-tokens-message" class="hidden mt-4 bg-yellow-900 border border-yellow-600 text-yellow-100 px-4 py-3 rounded-lg" role="alert"><p>Te has quedado sin energía para continuar. Tu voluntad se desvanece.</p><a href="{{ url_for('recharge_tokens') }}" class="font-bold text-yellow-300 hover:underline">Recarga tus tokens para seguir luchando contra la oscuridad.</a></div>
</div>
<script>
document.addEventListener('DOMContentLoaded', function() {
    const form = document.getElementById('action-form'), input = document.getElementById('action_input'), submitButton = document.getElementById('submit-action'), narrativeContainer = document.getElementById('story-container'), narrativeContent = document.getElementById('narrative-content'), loadingIndicator = document.getElementById('loading-indicator'), noTokensMessage = document.getElementById('no-tokens-message'), tokenCountSpan = document.getElementById('token-count');
    const audioPlayer = document.getElementById('narrator-audio-player');
    const playBtn = document.getElementById('play-btn'), pauseBtn = document.getElementById('pause-btn'), stopBtn = document.getElementById('stop-btn');
    narrativeContainer.scrollTop = narrativeContainer.scrollHeight;
    form.addEventListener('submit', function(e) {
        e.preventDefault(); const actionText = input.value.trim(); if (!actionText) return;
        audioPlayer.pause(); setLoading(true);
        fetch("{{ url_for('player_action') }}", { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ action: actionText }) })
        .then(response => { if (!response.ok) { throw new Error(`HTTP error! status: ${response.status}`); } return response.json(); })
        .then(data => {
            if (data.error) { handleError(data); } else {
                const playerHtml = `\\n\\n<span class="text-blue-400 font-bold">Tú: ${actionText}</span>`; appendNarrative(playerHtml);
                appendNarrative(`\\n\\n${data.narrative}`);
                tokenCountSpan.textContent = data.tokens;
                playAudio();
            }
        })
        .catch(error => { console.error('Fetch Error:', error); appendNarrative('\\n<span class="text-red-400 font-bold">Error de conexión. No se pudo contactar con el otro lado.</span>'); })
        .finally(() => { setLoading(false); input.value = ''; });
    });
    playBtn.addEventListener('click', () => { if (audioPlayer.src && audioPlayer.paused) { audioPlayer.play(); } else if (audioPlayer.src) { audioPlayer.currentTime = 0; audioPlayer.play(); } else { playAudio(); } });
    pauseBtn.addEventListener('click', () => { audioPlayer.pause(); });
    stopBtn.addEventListener('click', () => { audioPlayer.pause(); audioPlayer.currentTime = 0; });
    function playAudio() { audioPlayer.src = `{{ url_for('narrate') }}?t=${new Date().getTime()}`; audioPlayer.play().catch(e => console.error("Error al reproducir audio:", e)); }
    function setLoading(isLoading) { input.disabled = isLoading; submitButton.disabled = isLoading; loadingIndicator.classList.toggle('hidden', !isLoading); if (isLoading) narrativeContainer.scrollTop = narrativeContainer.scrollHeight; }
    function appendNarrative(html) { narrativeContent.innerHTML += html.replace(/\\n/g, '<br>'); narrativeContainer.scrollTop = narrativeContainer.scrollHeight; }
    function handleError(data) { if (data.reason === 'no_tokens') { noTokensMessage.classList.remove('hidden'); input.disabled = true; submitButton.disabled = true; } else { appendNarrative(`\\n<span class="text-red-400 font-bold">Error: ${data.error}</span>`); } }
});
</script>
"""

# ==============================================================================
# FUNCIÓN AUXILIAR PARA RENDERIZAR PÁGINAS
# ==============================================================================

def render_page(content_template, title, **context):
    full_html = BASE_TEMPLATE.replace('', content_template)
    return render_template_string(full_html, title=title, **context)

# ==============================================================================
# RUTAS DE LA APLICACIÓN
# ==============================================================================

@app.route('/')
def index():
    if 'user_id' in session: return redirect(url_for('menu'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session: return redirect(url_for('menu'))
    error = None
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']):
            session['user_id'] = user.id
            session['username'] = user.username
            session['user_tokens'] = user.tokens
            return redirect(url_for('menu'))
        else: error = "Usuario o contraseña incorrectos."
    return render_page(LOGIN_CONTENT, title="Iniciar Sesión", error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session: return redirect(url_for('menu'))
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
    return render_page(REGISTER_CONTENT, title="Registro", error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/menu')
def menu():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user: session['user_tokens'] = user.tokens
    return render_page(MENU_CONTENT, title="Menú Principal", game_modes=GAME_MODES)

@app.route('/start_game', methods=['POST'])
def start_game():
    if 'user_id' not in session: return redirect(url_for('login'))
    mode = request.form.get('mode')
    if mode not in GAME_MODES: return redirect(url_for('menu'))
    
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
    if 'user_id' not in session or 'game_session_id' not in session: return redirect(url_for('menu'))
    game = db.session.get(GameSession, session.get('game_session_id'))
    if not game or not game.is_active: return redirect(url_for('menu'))
    game_title = GAME_MODES[game.game_mode]['title']
    return render_page(GAME_CONTENT, title=game_title, initial_narrative=game.history)

@app.route('/action', methods=['POST'])
def player_action():
    if 'user_id' not in session or 'game_session_id' not in session: return jsonify({'error': 'Sesión no válida'}), 401
    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return jsonify({'error': 'Tu sesión ha expirado.'}), 401
    if user.tokens <= 0: return jsonify({'error': 'No tienes suficientes tokens.', 'reason': 'no_tokens'}), 403
    game = db.session.get(GameSession, session['game_session_id'])
    if not game or not game.is_active: return jsonify({'error': 'La sesión de juego no está activa.'}), 400
    action = request.json.get('action')
    if not action: return jsonify({'error': 'Acción no proporcionada.'}), 400
    
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
        # El audio_stream del cliente es un iterador listo para la respuesta
        return Response(audio_stream, mimetype='audio/mpeg')
    
    return "Error al generar audio", 500

@app.route('/recharge')
def recharge_tokens():
    if 'user_id' not in session: return redirect(url_for('login'))
    return render_page(RECHARGE_CONTENT, title="Recargar Tokens")

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    if 'user_id' not in session: return jsonify(error="No autenticado"), 401
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{'price_data': {'currency': 'mxn', 'product_data': {'name': 'Paquete de 20 Tokens Espectrales'},'unit_amount': 5000},'quantity': 1}],
            mode='payment',
            success_url=url_for('menu', _external=True, payment='success'),
            cancel_url=url_for('menu', _external=True, payment='cancelled'),
            metadata={'user_id': session['user_id']}
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e: return str(e)

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