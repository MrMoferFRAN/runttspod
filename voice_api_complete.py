#!/usr/bin/env python3
"""
Voice Cloning API Completa - CSM-1B
API robusta con estructura de carpetas organizadas por voz
"""

import os
import sys
import logging
import traceback
import json
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
import tempfile
import shutil

import torch
import torchaudio
import uvicorn
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import aiofiles
from transformers import CsmForConditionalGeneration, AutoProcessor
import numpy as np
from pydantic import BaseModel

# Fix for torch.compiler compatibility issues
# Some PyTorch versions don't have torch.compiler.is_compiling
if not hasattr(torch.compiler, 'is_compiling'):
    torch.compiler.is_compiling = lambda: False

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/voice_api.log', mode='a') if Path('logs').exists() else logging.NullHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuración del entorno
os.environ.setdefault('NO_TORCH_COMPILE', '1')
os.environ.setdefault('HF_TOKEN', '|==>REMOVED')

# Modelos Pydantic para respuestas
class VoiceProfile(BaseModel):
    name: str
    audio_path: str
    transcription: str
    language: str = "es"
    quality_score: float = 1.0
    duration: float
    sample_rate: int
    created_at: str

class VoiceCollection(BaseModel):
    voice_id: str
    profiles: List[VoiceProfile]
    total_samples: int
    average_duration: float
    created_at: str
    updated_at: str

class CSMVoiceManager:
    """Gestor completo de voces para CSM-1B"""
    
    def __init__(self, model_path: str = "./models/sesame-csm-1b", voices_dir: str = "./voices"):
        self.model_path = model_path
        self.voices_dir = Path(voices_dir)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        self.processor = None
        self.voice_collections = {}
        
        logger.info(f"🎤 Initializing CSM Voice Manager")
        logger.info(f"📁 Model path: {model_path}")
        logger.info(f"📁 Voices directory: {voices_dir}")
        logger.info(f"🖥️ Device: {self.device}")
        
        # Crear directorios necesarios
        self.voices_dir.mkdir(exist_ok=True)
        Path("outputs").mkdir(exist_ok=True)
        Path("temp").mkdir(exist_ok=True)
        Path("logs").mkdir(exist_ok=True)
        
        # Verificar modelo
        if not Path(model_path).exists():
            raise FileNotFoundError(f"Model directory not found: {model_path}")
        
        # Cargar modelo y voces
        self._load_model()
        self._load_voice_collections()
    
    def _load_model(self):
        """Carga el modelo y processor CSM-1B"""
        try:
            logger.info("📥 Loading CSM processor...")
            self.processor = AutoProcessor.from_pretrained(self.model_path)
            
            logger.info("📥 Loading CSM model...")
            self.model = CsmForConditionalGeneration.from_pretrained(
                self.model_path,
                device_map=self.device,
                torch_dtype=torch.float32
            )
            
            logger.info("✅ CSM model loaded successfully")
            
            if torch.cuda.is_available():
                gpu_info = torch.cuda.get_device_properties(0)
                memory_gb = gpu_info.total_memory / 1024**3
                logger.info(f"🖥️ GPU: {gpu_info.name} ({memory_gb:.1f} GB)")
            
        except Exception as e:
            logger.error(f"❌ Failed to load CSM model: {e}")
            raise
    
    def _load_voice_collections(self):
        """Carga todas las colecciones de voces desde el directorio voices/"""
        self.voice_collections = {}
        
        for voice_dir in self.voices_dir.iterdir():
            if voice_dir.is_dir():
                try:
                    collection = self._load_voice_collection(voice_dir.name)
                    if collection:
                        self.voice_collections[voice_dir.name] = collection
                        logger.info(f"✅ Loaded voice collection: {voice_dir.name} ({len(collection.profiles)} samples)")
                except Exception as e:
                    logger.error(f"❌ Failed to load voice collection {voice_dir.name}: {e}")
        
        logger.info(f"📢 Loaded {len(self.voice_collections)} voice collections")
    
    def _load_voice_collection(self, voice_id: str) -> Optional[VoiceCollection]:
        """Carga una colección de voz específica"""
        voice_path = self.voices_dir / voice_id
        profiles_file = voice_path / "profiles.json"
        
        if not voice_path.exists() or not profiles_file.exists():
            return None
        
        try:
            with open(profiles_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            profiles = []
            
            # Cargar perfiles existentes
            if 'voices' in data and isinstance(data['voices'], dict):
                # Formato legacy - un solo perfil
                profile_data = data['voices']
                profile = VoiceProfile(
                    name=profile_data.get('name', voice_id),
                    audio_path=profile_data['audio_path'],
                    transcription=profile_data['transcription'],
                    language=profile_data.get('language', 'es'),
                    quality_score=profile_data.get('quality_score', 1.0),
                    duration=profile_data['duration'],
                    sample_rate=profile_data['sample_rate'],
                    created_at=profile_data.get('created_at', datetime.now().isoformat())
                )
                profiles.append(profile)
            elif 'profiles' in data:
                # Formato nuevo - múltiples perfiles
                for profile_data in data['profiles']:
                    profile = VoiceProfile(**profile_data)
                    profiles.append(profile)
            
            # Calcular estadísticas
            total_samples = len(profiles)
            average_duration = sum(p.duration for p in profiles) / total_samples if total_samples > 0 else 0
            
            collection = VoiceCollection(
                voice_id=voice_id,
                profiles=profiles,
                total_samples=total_samples,
                average_duration=average_duration,
                created_at=data.get('created_at', datetime.now().isoformat()),
                updated_at=data.get('updated_at', datetime.now().isoformat())
            )
            
            return collection
            
        except Exception as e:
            logger.error(f"❌ Failed to load voice collection {voice_id}: {e}")
            return None
    
    def _save_voice_collection(self, voice_id: str, collection: VoiceCollection):
        """Guarda una colección de voz"""
        voice_path = self.voices_dir / voice_id
        voice_path.mkdir(exist_ok=True)
        
        profiles_file = voice_path / "profiles.json"
        
        data = {
            "voice_id": collection.voice_id,
            "profiles": [profile.dict() for profile in collection.profiles],
            "total_samples": collection.total_samples,
            "average_duration": collection.average_duration,
            "created_at": collection.created_at,
            "updated_at": datetime.now().isoformat()
        }
        
        with open(profiles_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    async def upload_voice_sample(
        self, 
        voice_id: str, 
        audio_file: UploadFile, 
        transcription: str = None,
        language: str = "es"
    ) -> VoiceProfile:
        """Sube una muestra de audio para una voz con normalización y validación"""
        
        # Usar el nombre del archivo como transcripción si no se proporciona
        if not transcription:
            transcription = Path(audio_file.filename).stem.replace('_', ' ').replace('-', ' ')
        
        # Crear directorio de la voz
        voice_path = self.voices_dir / voice_id
        voice_path.mkdir(exist_ok=True)
        
        # Generar nombre de archivo normalizado (siempre WAV)
        safe_name = "".join(c for c in transcription if c.isalnum() or c in (' ', '-', '_')).rstrip()
        safe_name = safe_name[:100]  # Limitar longitud
        
        # Siempre usar extensión .wav
        audio_filename = f"{safe_name}.wav"
        audio_path = voice_path / audio_filename
        temp_path = Path("temp") / f"upload_{audio_file.filename}"
        
        try:
            # Guardar archivo temporal
            async with aiofiles.open(temp_path, 'wb') as f:
                content = await audio_file.read()
                await f.write(content)
            
            # Cargar y analizar audio
            try:
                waveform, sample_rate = torchaudio.load(temp_path)
                original_duration = waveform.shape[1] / sample_rate
                
                logger.info(f"📊 Original audio: {original_duration:.2f}s, {sample_rate}Hz, {waveform.shape[0]} channels")
                
                # VALIDAR DURACIÓN (3-9 segundos)
                if original_duration < 3.0:
                    raise ValueError(f"Audio demasiado corto: {original_duration:.2f}s. Mínimo requerido: 3.0s")
                elif original_duration > 9.0:
                    raise ValueError(f"Audio demasiado largo: {original_duration:.2f}s. Máximo permitido: 9.0s")
                
                # NORMALIZAR AUDIO
                # 1. Convertir a mono si es estéreo
                if waveform.shape[0] > 1:
                    waveform = waveform.mean(dim=0, keepdim=True)
                    logger.info("🔄 Converted to mono")
                
                # 2. Resample a 24kHz si es necesario
                if sample_rate != 24000:
                    resampler = torchaudio.transforms.Resample(sample_rate, 24000)
                    waveform = resampler(waveform)
                    logger.info(f"🔄 Resampled from {sample_rate}Hz to 24000Hz")
                    sample_rate = 24000
                
                # 3. Normalizar amplitud (RMS normalization)
                rms = torch.sqrt(torch.mean(waveform**2))
                if rms > 0:
                    target_rms = 0.1  # Nivel de normalización
                    waveform = waveform * (target_rms / rms)
                    logger.info(f"🔄 Normalized RMS from {rms:.4f} to {target_rms:.4f}")
                
                # 4. Aplicar fade in/out suave para evitar clicks
                fade_samples = int(0.01 * sample_rate)  # 10ms fade
                if waveform.shape[1] > fade_samples * 2:
                    # Fade in
                    fade_in = torch.linspace(0, 1, fade_samples)
                    waveform[0, :fade_samples] *= fade_in
                    # Fade out
                    fade_out = torch.linspace(1, 0, fade_samples)
                    waveform[0, -fade_samples:] *= fade_out
                    logger.info("🔄 Applied fade in/out")
                
                # 5. Recalcular duración final
                duration = waveform.shape[1] / sample_rate
                
                # Guardar archivo normalizado en formato WAV 24kHz
                torchaudio.save(audio_path, waveform, sample_rate)
                logger.info(f"✅ Saved normalized audio: {duration:.2f}s, 24000Hz, mono")
                
            except Exception as e:
                logger.error(f"❌ Failed to process audio: {e}")
                raise ValueError(f"Error procesando audio: {str(e)}")
                
        finally:
            # Limpiar archivo temporal
            if temp_path.exists():
                temp_path.unlink()
        
        # Crear perfil
        profile = VoiceProfile(
            name=safe_name,
            audio_path=str(audio_path),
            transcription=transcription,
            language=language,
            quality_score=1.0,
            duration=duration,
            sample_rate=sample_rate,
            created_at=datetime.now().isoformat()
        )
        
        # Cargar o crear colección
        collection = self.voice_collections.get(voice_id)
        if not collection:
            collection = VoiceCollection(
                voice_id=voice_id,
                profiles=[],
                total_samples=0,
                average_duration=0.0,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat()
            )
        
        # Agregar perfil a la colección
        collection.profiles.append(profile)
        collection.total_samples = len(collection.profiles)
        collection.average_duration = sum(p.duration for p in collection.profiles) / collection.total_samples
        collection.updated_at = datetime.now().isoformat()
        
        # Guardar colección
        self._save_voice_collection(voice_id, collection)
        self.voice_collections[voice_id] = collection
        
        logger.info(f"✅ Added voice sample to {voice_id}: {safe_name}")
        return profile
    
    def clone_voice(
        self, 
        text: str, 
        voice_id: str = None,
        sample_name: str = None,
        temperature: float = 0.8,
        max_tokens: int = 512
    ) -> np.ndarray:
        """Clona una voz usando una muestra específica"""
        try:
            conversation = []
            
            # Buscar muestra de referencia
            if voice_id and voice_id in self.voice_collections:
                collection = self.voice_collections[voice_id]
                
                # Buscar muestra específica o usar la primera
                target_profile = None
                if sample_name:
                    target_profile = next((p for p in collection.profiles if p.name == sample_name), None)
                
                if not target_profile and collection.profiles:
                    target_profile = collection.profiles[0]  # Usar la primera muestra
                
                if target_profile:
                    # Cargar audio de referencia
                    try:
                        waveform, sample_rate = torchaudio.load(target_profile.audio_path)
                        
                        # Resample a 24kHz si es necesario
                        if sample_rate != 24000:
                            resampler = torchaudio.transforms.Resample(sample_rate, 24000)
                            waveform = resampler(waveform)
                        
                        # Convertir a mono si es estéreo
                        if waveform.shape[0] > 1:
                            waveform = waveform.mean(dim=0, keepdim=True)
                        
                        conversation.append({
                            "role": "0",
                            "content": [
                                {"type": "text", "text": target_profile.transcription},
                                {"type": "audio", "path": waveform.squeeze().numpy()}
                            ]
                        })
                        
                        logger.info(f"🎯 Using voice reference: {voice_id}/{target_profile.name}")
                        
                    except Exception as e:
                        logger.error(f"❌ Failed to load reference audio: {e}")
            
            # Agregar texto a sintetizar
            conversation.append({
                "role": "0",
                "content": [{"type": "text", "text": text}]
            })
            
            # Procesar entrada
            if conversation:
                inputs = self.processor.apply_chat_template(
                    conversation,
                    tokenize=True,
                    return_dict=True,
                ).to(self.device)
            else:
                # Sin contexto, usar formato simple
                formatted_text = f"[0]{text}"
                inputs = self.processor(formatted_text, add_special_tokens=True).to(self.device)
            
            # Generar audio
            with torch.no_grad():
                outputs = self.model.generate(
                    **inputs, 
                    output_audio=True,
                    max_new_tokens=max_tokens,
                    temperature=temperature,
                    do_sample=True
                )
            
            # Extraer y procesar audio
            if hasattr(outputs, 'audio_values'):
                audio = outputs.audio_values
            elif isinstance(outputs, dict) and 'audio_values' in outputs:
                audio = outputs['audio_values']
            elif isinstance(outputs, (list, tuple)) and len(outputs) > 1:
                audio = outputs[1] if len(outputs) > 1 else outputs[0]
            else:
                audio = outputs
            
            # Convertir a numpy
            if isinstance(audio, torch.Tensor):
                audio = audio.float().cpu().numpy()
            elif isinstance(audio, list):
                if len(audio) > 0:
                    audio = audio[0]
                    if isinstance(audio, torch.Tensor):
                        audio = audio.float().cpu().numpy()
                    else:
                        audio = np.array(audio, dtype=np.float32)
                else:
                    logger.warning("⚠️ Model returned empty audio, generating silence")
                    audio = np.zeros(24000, dtype=np.float32)
            else:
                audio = np.array(audio, dtype=np.float32)
            
            # Procesar audio final
            if len(audio.shape) > 1:
                audio = audio.flatten()
            
            if np.max(np.abs(audio)) > 1.0:
                audio = audio / np.max(np.abs(audio))
            
            logger.info(f"✅ Generated audio shape: {audio.shape}, dtype: {audio.dtype}")
            return audio
            
        except Exception as e:
            logger.error(f"❌ Voice cloning failed: {e}")
            raise

# Inicializar manager global
voice_manager = None

def get_voice_manager():
    """Obtiene la instancia global del manager"""
    global voice_manager
    if voice_manager is None:
        voice_manager = CSMVoiceManager()
    return voice_manager

# Configurar FastAPI
app = FastAPI(
    title="🎤 Voice Cloning API Complete - CSM-1B",
    description="API completa de clonación de voz con gestión avanzada de perfiles organizados por carpetas",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    """Inicialización del servidor"""
    logger.info("🚀 Starting Voice Cloning API Complete...")
    
    try:
        get_voice_manager()
        logger.info("✅ Voice Cloning API Complete ready")
    except Exception as e:
        logger.error(f"❌ Failed to initialize voice manager: {e}")
        raise

@app.get("/", response_class=HTMLResponse)
async def home():
    """Página principal mejorada"""
    return """
    <html>
        <head>
            <title>🎤 Voice Cloning API Complete - CSM-1B</title>
            <style>
                body { font-family: 'Segoe UI', sans-serif; margin: 0; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
                .container { max-width: 1000px; margin: 0 auto; padding: 40px 20px; }
                .header { background: rgba(255,255,255,0.95); padding: 30px; border-radius: 15px; margin-bottom: 30px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
                h1 { color: #333; text-align: center; margin: 0; font-size: 2.5em; }
                .subtitle { text-align: center; color: #666; margin-top: 10px; font-size: 1.2em; }
                .section { background: rgba(255,255,255,0.95); margin: 20px 0; padding: 25px; border-radius: 15px; box-shadow: 0 5px 15px rgba(0,0,0,0.1); }
                .endpoint { background: #f8f9fa; padding: 15px; margin: 10px 0; border-radius: 8px; font-family: 'Consolas', monospace; border-left: 4px solid #007bff; }
                .method { background: #007bff; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; margin-right: 10px; }
                .method.post { background: #28a745; }
                .method.get { background: #17a2b8; }
                a { color: #007bff; text-decoration: none; }
                a:hover { text-decoration: underline; }
                .status { padding: 15px; background: linear-gradient(45deg, #d4edda, #c3e6cb); border-radius: 8px; color: #155724; text-align: center; font-weight: bold; }
                .features { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-top: 20px; }
                .feature { background: rgba(255,255,255,0.9); padding: 20px; border-radius: 10px; text-align: center; border: 2px solid #eee; }
                .feature h3 { color: #333; margin-top: 0; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🎤 Voice Cloning API Complete</h1>
                    <div class="subtitle">Powered by CSM-1B • Gestión Avanzada de Voces</div>
                </div>
                
                <div class="section">
                    <div class="status">
                        ✅ API funcionando perfectamente • Sistema de carpetas organizadas
                    </div>
                </div>
                
                <div class="section">
                    <h2>🚀 Características Principales</h2>
                    <div class="features">
                        <div class="feature">
                            <h3>📁 Gestión por Carpetas</h3>
                            <p>Cada voz tiene su propia carpeta con múltiples muestras</p>
                        </div>
                        <div class="feature">
                            <h3>📤 Upload Inteligente</h3>
                            <p>Validación automática: 3-9s, WAV 24kHz mono normalizado</p>
                        </div>
                        <div class="feature">
                            <h3>🎯 Clonación Precisa</h3>
                            <p>Selección específica de muestras para mejor calidad</p>
                        </div>
                        <div class="feature">
                            <h3>📊 Análisis Completo</h3>
                            <p>Estadísticas detalladas y métricas de calidad</p>
                        </div>
                    </div>
                </div>
                
                <div class="section">
                    <h2>📋 Endpoints de la API</h2>
                    <div class="endpoint"><span class="method get">GET</span>/health - Estado del sistema</div>
                    <div class="endpoint"><span class="method get">GET</span>/voices - Listar todas las colecciones de voces</div>
                    <div class="endpoint"><span class="method get">GET</span>/voices/{voice_id} - Detalles de una voz específica</div>
                    <div class="endpoint"><span class="method post">POST</span>/voices/{voice_id}/upload - Subir muestra de audio</div>
                    <div class="endpoint"><span class="method post">POST</span>/clone - Clonar voz con texto</div>
                    <div class="endpoint"><span class="method get">GET</span>/docs - Documentación interactiva</div>
                </div>
                
                <div class="section">
                    <h2>🔗 Enlaces Rápidos</h2>
                    <p><a href="/docs">📖 Documentación Interactiva (Swagger UI)</a></p>
                    <p><a href="/health">🔍 Health Check</a></p>
                    <p><a href="/voices">📢 Ver Todas las Voces</a></p>
                </div>
                
                <div class="section">
                    <h2>💡 Ejemplo de Uso</h2>
                    <div class="endpoint">
                        # Subir muestra de voz (3-9s, será normalizado a WAV 24kHz mono)<br>
                        curl -X POST 'http://localhost:7860/voices/fran-fem/upload' \\<br>
                        &nbsp;&nbsp;&nbsp;&nbsp;-F 'audio_file=@audio.wav' \\<br>
                        &nbsp;&nbsp;&nbsp;&nbsp;-F 'transcription=Hola mundo'
                    </div>
                    <div class="endpoint">
                        # Clonar voz<br>
                        curl -X POST 'http://localhost:7860/clone' \\<br>
                        &nbsp;&nbsp;&nbsp;&nbsp;-F 'text=Texto a sintetizar' \\<br>
                        &nbsp;&nbsp;&nbsp;&nbsp;-F 'voice_id=fran-fem'
                    </div>
                </div>
            </div>
        </body>
    </html>
    """

@app.get("/health")
async def health_check():
    """Health check mejorado"""
    try:
        manager = get_voice_manager()
        gpu_available = torch.cuda.is_available()
        
        # Estadísticas detalladas
        total_voices = len(manager.voice_collections)
        total_samples = sum(len(collection.profiles) for collection in manager.voice_collections.values())
        
        gpu_info = {}
        if gpu_available:
            gpu_props = torch.cuda.get_device_properties(0)
            gpu_info = {
                "name": gpu_props.name,
                "memory_gb": gpu_props.total_memory / 1024**3,
                "memory_used_gb": torch.cuda.memory_allocated() / 1024**3
            }
        
        return {
            "status": "healthy",
            "model_loaded": manager.model is not None,
            "processor_loaded": manager.processor is not None,
            "gpu_available": gpu_available,
            "gpu_info": gpu_info,
            "voice_collections": total_voices,
            "total_voice_samples": total_samples,
            "device": manager.device,
            "model_path": manager.model_path,
            "voices_directory": str(manager.voices_dir)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")

@app.get("/voices")
async def list_voice_collections():
    """Lista todas las colecciones de voces"""
    try:
        manager = get_voice_manager()
        
        collections_summary = {}
        for voice_id, collection in manager.voice_collections.items():
            collections_summary[voice_id] = {
                "total_samples": collection.total_samples,
                "average_duration": round(collection.average_duration, 2),
                "created_at": collection.created_at,
                "updated_at": collection.updated_at,
                "samples": [
                    {
                        "name": profile.name,
                        "transcription": profile.transcription,
                        "duration": round(profile.duration, 2),
                        "language": profile.language
                    }
                    for profile in collection.profiles
                ]
            }
        
        return {
            "voice_collections": collections_summary,
            "total_collections": len(collections_summary),
            "total_samples": sum(c["total_samples"] for c in collections_summary.values())
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list voices: {str(e)}")

@app.get("/voices/{voice_id}")
async def get_voice_collection(voice_id: str):
    """Obtiene detalles de una colección de voz específica"""
    try:
        manager = get_voice_manager()
        
        if voice_id not in manager.voice_collections:
            raise HTTPException(status_code=404, detail=f"Voice collection '{voice_id}' not found")
        
        collection = manager.voice_collections[voice_id]
        return collection.dict()
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get voice collection: {str(e)}")

@app.post("/voices/{voice_id}/upload")
async def upload_voice_sample(
    voice_id: str,
    audio_file: UploadFile = File(..., description="Audio file"),
    transcription: Optional[str] = Form(None, description="Audio transcription (optional - uses filename if not provided)"),
    language: str = Form("es", description="Language code")
):
    """Sube una muestra de audio para una voz específica"""
    try:
        manager = get_voice_manager()
        
        # Validar archivo de audio
        valid_extensions = {'.wav', '.mp3', '.flac', '.ogg', '.m4a'}
        file_extension = Path(audio_file.filename).suffix.lower()
        
        if not (audio_file.content_type and audio_file.content_type.startswith('audio/')) and file_extension not in valid_extensions:
            raise HTTPException(status_code=400, detail=f"File must be an audio file. Supported formats: {', '.join(valid_extensions)}")
        
        # Información sobre requisitos
        logger.info(f"📤 Processing upload for voice '{voice_id}': {audio_file.filename}")
        logger.info("📋 Requirements: 3-9 seconds duration, will be normalized to WAV 24kHz mono")
        
        # Subir muestra
        profile = await manager.upload_voice_sample(
            voice_id=voice_id,
            audio_file=audio_file,
            transcription=transcription,
            language=language
        )
        
        return {
            "message": f"Voice sample uploaded successfully to '{voice_id}'",
            "profile": profile.dict(),
            "collection_stats": {
                "total_samples": manager.voice_collections[voice_id].total_samples,
                "average_duration": round(manager.voice_collections[voice_id].average_duration, 2)
            }
        }
        
    except HTTPException:
        raise
    except ValueError as e:
        # Errores de validación específicos (duración, formato, etc.)
        logger.error(f"❌ Validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"❌ Voice upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Voice upload failed: {str(e)}")

@app.post("/clone")
async def clone_voice_endpoint(
    text: str = Form(..., description="Text to synthesize"),
    voice_id: Optional[str] = Form(None, description="Voice collection ID"),
    sample_name: Optional[str] = Form(None, description="Specific sample name (optional)"),
    temperature: float = Form(0.8, description="Sampling temperature"),
    max_tokens: int = Form(512, description="Maximum tokens to generate"),
    output_format: str = Form("wav", description="Output format (wav)")
):
    """Clona una voz con el texto especificado"""
    try:
        manager = get_voice_manager()
        
        # Validar voice_id si se proporciona
        if voice_id and voice_id not in manager.voice_collections:
            raise HTTPException(status_code=404, detail=f"Voice collection '{voice_id}' not found")
        
        # Generar audio
        audio = manager.clone_voice(
            text=text,
            voice_id=voice_id,
            sample_name=sample_name,
            temperature=temperature,
            max_tokens=max_tokens
        )
        
        # Crear nombre de archivo único
        text_hash = hashlib.md5(text.encode()).hexdigest()[:8]
        voice_suffix = f"_{voice_id}" if voice_id else "_default"
        sample_suffix = f"_{sample_name}" if sample_name else ""
        filename = f"cloned{voice_suffix}{sample_suffix}_{text_hash}.{output_format}"
        
        output_path = Path("outputs") / filename
        
        # Guardar audio
        if isinstance(audio, np.ndarray):
            audio = audio.astype(np.float32)
            audio_tensor = torch.from_numpy(audio)
        else:
            audio_tensor = audio.float()
        
        if len(audio_tensor.shape) == 1:
            audio_tensor = audio_tensor.unsqueeze(0)
        
        # Use soundfile instead of torchaudio for better compatibility
        audio_numpy = audio_tensor.squeeze().numpy() if isinstance(audio_tensor, torch.Tensor) else audio_tensor
        import soundfile as sf
        sf.write(output_path, audio_numpy, 24000)
        
        logger.info(f"✅ Generated audio: {output_path}")
        
        return FileResponse(
            path=output_path,
            media_type="audio/wav",
            filename=filename
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Voice cloning failed: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning failed: {str(e)}")

if __name__ == "__main__":
    logger.info("🎤 Voice Cloning API Complete - Starting...")
    logger.info("🔍 Checking system requirements...")
    
    # Verificar GPU
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name()
        gpu_memory = torch.cuda.get_device_properties(0).total_memory / 1024**3
        logger.info(f"✅ GPU Available: {gpu_name} ({gpu_memory:.1f} GB)")
    else:
        logger.warning("⚠️ No GPU available, using CPU")
    
    # Verificar modelo
    model_path = Path("./models/sesame-csm-1b")
    if model_path.exists():
        logger.info("✅ Model directory found")
    else:
        logger.error("❌ Model directory not found")
        sys.exit(1)
    
    try:
        # Inicializar sistema
        logger.info("🎤 Setting up voice management system...")
        manager = get_voice_manager()
        
        logger.info(f"📢 Loaded {len(manager.voice_collections)} voice collections")
        for voice_id, collection in manager.voice_collections.items():
            logger.info(f"  • {voice_id}: {collection.total_samples} samples")
        
        logger.info("🚀 Starting server on http://0.0.0.0:7860")
        logger.info("📖 API Documentation: http://0.0.0.0:7860/docs")
        
        # Iniciar servidor
        uvicorn.run(
            "voice_api_complete:app",
            host="0.0.0.0",
            port=7860,
            log_level="info",
            access_log=True
        )
        
    except Exception as e:
        logger.error(f"❌ Failed to start server: {e}")
        traceback.print_exc()
        sys.exit(1) 