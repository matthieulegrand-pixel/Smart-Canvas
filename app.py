import os
import json
import re
from flask import Flask, request, render_template, jsonify
from werkzeug.utils import secure_filename

# --- NOUVEAUX IMPORTS VERTEX AI ---
import vertexai
from vertexai.generative_models import GenerativeModel, Part

app = Flask(__name__)

# --- CONFIGURATION DES DOSSIERS ---
DATA_DIR = 'saved_canvases'
UPLOAD_DIR = 'uploads'
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- CONFIGURATION VERTEX AI ---
# Remplacez par votre Project ID GCP. En production, utilisez les variables d'environnement.
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "votre-project-id-gcp")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "europe-west1") # Paris ou zone validée Airbus
vertexai.init(project=PROJECT_ID, location=LOCATION)

# Utilisation de Gemini 1.5 Pro pour sa grande fenêtre de contexte (idéal pour les manuels complexes)
model = GenerativeModel("gemini-1.5-pro-preview-0409") 

def get_master_prompt(doc_type):
    """Génère le prompt strict basé sur le type de document et les règles Airbus."""
    
    base_rules = """
    Tu es un Expert Architecte Système Airbus. Ta mission est d'extraire les données de ce document PDF et de les formater STRICTEMENT selon le schéma JSON fourni.
    
    RÈGLES STRICTES DE PROMPT ENGINEERING (GARDE-FOUS) :
    1. Identité (owner / authorizer) : N'extrais AUCUN titre ou fonction. Uniquement Prénom et Nom. Si colonnes mélangées par l'OCR, le premier nom trouvé est l'owner, le second est l'authorizer.
    2. globalRules : Extraire les codes Airbus (A-codes, M-codes). INTERDICTION FORMELLE d'extraire les codes "ABR" (Airbus Business Requirements).
    3. purpose : Résumé de 2 phrases MAXIMUM de la section PURPOSE/SCOPE. Exclure toute mention de ce que le document "ne couvre pas".
    """
    
    if doc_type in ['method', 'manual']:
        specific_rules = """
        4. mermaidChart : Utilise une topologie Mermaid TD. Des boucles (-.->) SONT AUTORISÉES uniquement si elles sont explicitement écrites dans le texte métier.
        5. Verbosité (how) : Ne SUR-SYNTHÉTISE PAS le champ 'how'. Extraire les paragraphes complets, les exemples et les astuces. Utilise \\n\\n pour séparer les paragraphes.
        """
    else:
        specific_rules = """
        4. mermaidChart : La topologie doit être STRICTEMENT VERTICALE et LINÉAIRE (A --> B). AUCUN branchement latéral. AUCUN subgraph.
        """

    json_schema = """
    RÉPOND UNIQUEMENT AVEC CE SCHÉMA JSON VALIDE, SANS MARKDOWN NI TEXTE AUTOUR :
    {
      "metadata": { "procedureId": "string", "issue": "string", "title": "string", "owner": "string", "authorizer": "string", "purpose": "string", "applicability": "string", "globalRules": ["string"], "triggers": ["string"], "synthesis": "string", "type": "operational|framework|method|manual" },
      "visuals": { "mermaidChart": "string" },
      "concepts": [ { "term": "string", "definition": "string" } ],
      "roles": [ { "name": "string", "description": "string" } ],
      "phases": [ 
        { "id": 1, "title": "string", "accountable": "string", "responsible": "string", "consulted": "string", "informed": "string", "inputs": ["string"], "deliverables": ["string"], "tasks": [ { "rule": "string", "how": "string" } ] } 
      ],
      "checklists": [ { "criterion": "string", "checkLogic": "string" } ],
      "toolsAndLibraries": ["string"]
    }
    """
    return base_rules + specific_rules + json_schema

# --- ROUTES EXISTANTES (MODIFIÉES) ---

@app.route('/', methods=['GET'])
def index():
    saved_files = []
    for filename in os.listdir(DATA_DIR):
        if filename.endswith('.json'):
            file_path = os.path.join(DATA_DIR, filename)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    metadata = data.get('metadata', {})
                    metadata['filename'] = filename
                    saved_files.append(metadata)
            except Exception as e:
                print(f"Erreur de lecture {filename}: {e}")
    return render_template('library.html', documents=saved_files)

@app.route('/generator', methods=['GET'])
def generator():
    # Cette page contiendra désormais un formulaire d'upload de fichier multipart/form-data
    return render_template('upload.html')

# --- NOUVELLE ROUTE : GÉNÉRATION DEPUIS LE PDF VIA VERTEX AI ---
@app.route('/generate_from_pdf', methods=['POST'])
def generate_from_pdf():
    if 'pdf_file' not in request.files:
        return "No file part", 400
    
    file = request.files['pdf_file']
    doc_type = request.form.get('doc_type', 'operational') # Type sélectionné dans l'UI

    if file.filename == '':
        return "No selected file", 400

    if file and file.filename.endswith('.pdf'):
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_DIR, filename)
        file.save(filepath)

        try:
            # 1. Préparation du PDF pour Vertex AI
            with open(filepath, "rb") as f:
                pdf_data = f.read()
            pdf_part = Part.from_data(data=pdf_data, mime_type="application/pdf")

            # 2. Récupération du Prompt métier
            prompt = get_master_prompt(doc_type)

            # 3. Appel à Gemini (Vertex AI) avec obligation de répondre en JSON
            generation_config = {"response_mime_type": "application/json"}
            response = model.generate_content(
                [pdf_part, prompt],
                generation_config=generation_config
            )

            # 4. Nettoyage du JSON généré (au cas où)
            clean_json = response.text.strip()
            if clean_json.startswith("```"):
                lines = clean_json.split("\n")
                clean_json = "\n".join(lines[1:-1])
            if clean_json.startswith("json"):
                clean_json = clean_json[4:].strip()

            # Nettoyage du fichier temporaire
            os.remove(filepath)

            # 5. Rendu du Canvas
            return render_template('canvas.html', app_data=clean_json)

        except Exception as e:
            # En cas d'erreur de l'API, on supprime le fichier et on renvoie l'erreur
            if os.path.exists(filepath):
                os.remove(filepath)
            return jsonify({"status": "error", "message": f"Vertex AI Error: {str(e)}"}), 500

    return "Invalid file format. Please upload a PDF.", 400

# Conserver l'ancienne route /generate pour le mode manuel/fallback si nécessaire
@app.route('/generate', methods=['POST'])
def generate():
    json_input = request.form.get('manual_json')
    if not json_input:
        return "No JSON data provided.", 400

    clean_json = json_input.strip()
    clean_json = re.sub(r'\[cite[^\]]*\]', '', clean_json)

    if clean_json.startswith("```"):
        lines = clean_json.split("\n")
        if len(lines) > 2:
            clean_json = "\n".join(lines[1:-1])
    if clean_json.startswith("json"):
        clean_json = clean_json[4:].strip()

    return render_template('canvas.html', app_data=clean_json)

@app.route('/save', methods=['POST'])
def save_canvas():
    data = request.json
    try:
        proc_id = data.get('metadata', {}).get('procedureId', 'Draft').replace('/', '_').replace(' ', '_')
        file_path = os.path.join(DATA_DIR, f"{proc_id}.json")
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            
        return jsonify({"status": "success", "message": "Saved to library!"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/view/<filename>', methods=['GET'])
def view_canvas(filename):
    file_path = os.path.join(DATA_DIR, filename)
    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_json = f.read()
        return render_template('canvas.html', app_data=raw_json)
    return "Document introuvable", 404

@app.route('/delete/<filename>', methods=['DELETE'])
def delete_canvas(filename):
    file_path = os.path.join(DATA_DIR, filename)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            return jsonify({"status": "success", "message": "File deleted"}), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "error", "message": "File not found"}), 404

@app.route('/rename/<filename>', methods=['POST'])
def rename_canvas(filename):
    data = request.json
    new_title = data.get('new_title')
    
    file_path = os.path.join(DATA_DIR, filename)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                canvas_data = json.load(f)
            
            if new_title:
                canvas_data['metadata']['title'] = new_title
                
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(canvas_data, f, ensure_ascii=False, indent=4)
                
            return jsonify({"status": "success", "message": "Document renamed"}), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "error", "message": "File not found"}), 404

if __name__ == '__main__':
    # Configuration pour le serveur de développement
    app.run(debug=True, host='0.0.0.0', port=8080)