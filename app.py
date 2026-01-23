# This is a simple web application using Flask to allow users to upload logos and QR codes to client photos.
# Uses Vercel Blob for file storage to bypass serverless function payload limits.

from flask import Flask, request, send_file, render_template, jsonify, send_from_directory, make_response
from PIL import Image, ImageDraw, ImageFont, ImageOps
import os
import zipfile
import io
import shutil
import qrcode
import requests
import json
import google.generativeai as genai
import time
from io import BytesIO
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static', static_url_path='/static')

# CORS support for Vercel
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# Use /tmp for Vercel serverless environment
IS_VERCEL = os.environ.get('VERCEL', False)
BLOB_READ_WRITE_TOKEN = os.environ.get('BLOB_READ_WRITE_TOKEN', '')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY', '')

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

if IS_VERCEL:
    UPLOAD_FOLDER = '/tmp/uploads/'
    PROCESSED_FOLDER = '/tmp/processed/'
else:
    UPLOAD_FOLDER = 'uploads/'
    PROCESSED_FOLDER = 'processed/'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['PROCESSED_FOLDER'] = PROCESSED_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size


def ensure_directory(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        print(f"Warning: Could not create directory {path}: {e}")


def clear_directory(path: str) -> None:
    ensure_directory(path)
    try:
        for entry in os.listdir(path):
            entry_path = os.path.join(path, entry)
            if os.path.isdir(entry_path):
                shutil.rmtree(entry_path)
            else:
                os.remove(entry_path)
    except Exception as e:
        print(f"Warning: Could not clear directory {path}: {e}")


# Only create directories if not in module import context
try:
    ensure_directory(UPLOAD_FOLDER)
    ensure_directory(PROCESSED_FOLDER)
except Exception as e:
    print(f"Warning: Directory initialization failed: {e}")

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS



def load_font(size: int) -> ImageFont.FreeTypeFont:
    # Get the directory where app.py is located
    app_dir = os.path.dirname(os.path.abspath(__file__))
    
    font_candidates = [
        os.path.join(app_dir, 'templates', 'fonts', 'Outfit-SemiBold.ttf'),
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
    ]

    for candidate in font_candidates:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue

    return ImageFont.load_default()


def draw_footer_text(draw, lines, footer_rect, font_size_base):
    if not lines:
        return

    # Use a slightly smaller font for secondary lines (90% of main)
    font_main = load_font(int(font_size_base))
    font_sub = load_font(int(font_size_base * 0.9))
    
    line_spacing = int(font_size_base * 0.3)
    
    # Calculate total height of text block
    total_text_height = 0
    line_data = []
    
    for i, line in enumerate(lines):
        font = font_main if i == 0 else font_sub
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        line_data.append({'text': line, 'font': font, 'w': w, 'h': h})
        total_text_height += h
        if i < len(lines) - 1:
            total_text_height += line_spacing

    # Center vertically in the footer
    footer_center_x = (footer_rect[0] + footer_rect[2]) // 2
    current_y = (footer_rect[1] + footer_rect[3]) // 2 - (total_text_height // 2)
    
    for data in line_data:
        x = footer_center_x - (data['w'] // 2)
        draw.text((x, current_y), data['text'], font=data['font'], fill=(0, 0, 0, 255))
        current_y += data['h'] + line_spacing



def upload_to_blob(file_content, filename, content_type='image/jpeg'):
    """Upload a file to Vercel Blob storage"""
    if not BLOB_READ_WRITE_TOKEN:
        raise Exception("BLOB_READ_WRITE_TOKEN not configured")
    
    headers = {
        'Authorization': f'Bearer {BLOB_READ_WRITE_TOKEN}',
        'Content-Type': content_type,
        'x-api-version': '7',
    }
    
    # Use the Vercel Blob API
    response = requests.put(
        f'https://blob.vercel-storage.com/{filename}',
        headers=headers,
        data=file_content
    )
    
    if response.status_code == 200:
        result = response.json()
        return result.get('url')
    else:
        raise Exception(f"Failed to upload to Blob: {response.status_code} - {response.text}")


def download_from_url(url):
    """Download a file from a URL"""
    response = requests.get(url)
    if response.status_code == 200:
        return BytesIO(response.content)
    else:
        raise Exception(f"Failed to download from URL: {response.status_code}")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/blob-token', methods=['GET'])
def get_blob_token():
    """Return the blob token for client-side uploads"""
    if not BLOB_READ_WRITE_TOKEN:
        return jsonify({'error': 'Blob storage not configured'}), 500
    return jsonify({'token': BLOB_READ_WRITE_TOKEN})


@app.route('/api/generate-image', methods=['POST'])
def generate_image():
    """Generate images using Imagen 3 via Google AI Studio"""
    try:
        if not GOOGLE_API_KEY:
            return jsonify({'error': 'GOOGLE_API_KEY not configured'}), 500
            
        data = request.get_json()
        if not data or not data.get('prompt'):
            return jsonify({'error': 'No prompt provided'}), 400
            
        prompt = data.get('prompt')
        count = min(max(int(data.get('count', 1)), 1), 10)
        
        # Using Imagen 3 for high-quality generation
        # "imagen-3.0-generate-001" is the current standard in AI Studio
        model = genai.GenerativeModel("imagen-3.0-generate-001")
        
        generated_files = []
        
        # Generate requested number of images
        for i in range(count):
            response = model.generate_content(prompt)
            
            if not response.candidates or not response.candidates[0].content.parts:
                continue
                
            # Extract the image data
            image_part = next((part for part in response.candidates[0].content.parts if part.inline_data), None)
            if not image_part:
                continue
                
            image_bytes = image_part.inline_data.data
            
            # Upload to Vercel Blob
            ts = int(time.time())
            filename = f"generated_{secure_filename(prompt[:20])}_{ts}_{i}.jpg"
            blob_url = upload_to_blob(image_bytes, filename, 'image/jpeg')
            
            generated_files.append({
                'url': blob_url,
                'filename': filename
            })
        
        if not generated_files:
            return jsonify({'error': 'Failed to generate any images. Check your prompt safety or API quota.'}), 500
            
        return jsonify({
            'success': True,
            'files': generated_files
        })
        
    except Exception as e:
        print(f"Error generating image: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/process', methods=['POST'])
def process_photos():
    """Process photos from Blob URLs"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        photo_urls = data.get('photo_urls', [])
        logo_url = data.get('logo_url')
        qr_url = data.get('qr_url')
        practice_name = data.get('practice_name', '').strip()
        plus_code = data.get('plus_code', '').strip()
        file_names = data.get('file_names', [])
        
        if not photo_urls:
            return jsonify({'error': 'No photos provided'}), 400
        
        # Download and prepare logo if provided
        logo_img = None
        if logo_url:
            try:
                logo_data = download_from_url(logo_url)
                logo_img = Image.open(logo_data).convert('RGBA')
            except Exception as e:
                print(f"Error downloading logo: {e}")
        
        # Generate QR code if URL provided
        qr_img = None
        if qr_url:
            try:
                # Clean Google Maps URL if needed
                is_google_maps = any(domain in qr_url for domain in ['google.com/maps', 'maps.google.com', 'maps.app.goo.gl'])
                if is_google_maps and '?' in qr_url:
                    qr_url = qr_url.split('?')[0]
                
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_H,
                    box_size=10,
                    border=4,
                )
                qr.add_data(qr_url)
                qr.make(fit=True)
                qr_img = qr.make_image(fill_color="black", back_color="white").convert('RGBA')
            except Exception as e:
                print(f"Error generating QR code: {e}")
        
        processed_urls = []
        
        for i, photo_url in enumerate(photo_urls):
            try:
                # Download photo from Blob
                photo_data = download_from_url(photo_url)
                img = Image.open(photo_data)
                
                # Fix orientation based on EXIF data
                img = ImageOps.exif_transpose(img)
                img = img.convert('RGBA')
                
                # Clear metadata by creating a new image
                clean_img = Image.new('RGBA', img.size, (255, 255, 255, 255))
                clean_img.paste(img, (0, 0), img)
                
                # Calculate footer height (around 15% of image height, minimum 120px)
                footer_height = max(int(img.height * 0.15), 120)
                new_size = (img.width, img.height + footer_height)
                
                # Create new white canvas
                clean_img = Image.new('RGBA', new_size, (255, 255, 255, 255))
                clean_img.paste(img, (0, 0), img)
                
                draw = ImageDraw.Draw(clean_img)
                footer_y_start = img.height
                side_padding = int(img.width * 0.02)
                top_bottom_padding = int(footer_height * 0.1)
                content_height = footer_height - (top_bottom_padding * 2)
                
                # Define consistent fixed height for top elements (360px)
                target_element_height = 360
                
                # Process logo if provided (Top Left)
                if logo_img:
                    try:
                        logo_copy = logo_img.copy()
                        logo_copy.thumbnail((int(img.width * 0.3), target_element_height), Image.LANCZOS)
                        
                        # Apply 50% opacity to logo
                        alpha = logo_copy.split()[3]
                        alpha = alpha.point(lambda p: int(p * 0.5))
                        logo_copy.putalpha(alpha)
                        
                        # Position at Top Left with padding
                        clean_img.paste(logo_copy, (side_padding, side_padding), logo_copy)
                    except Exception as e:
                        print(f"Error processing logo: {e}")
                
                # Process QR code if provided (Top Right)
                if qr_img:
                    try:
                        qr_copy = qr_img.copy()
                        qr_copy.thumbnail((target_element_height, target_element_height), Image.LANCZOS)
                        
                        # Apply 50% opacity to QR code
                        alpha = qr_copy.split()[3]
                        alpha = alpha.point(lambda p: int(p * 0.5))
                        qr_copy.putalpha(alpha)
                        
                        # Position at Top Right with padding
                        qr_x = img.width - qr_copy.width - side_padding
                        clean_img.paste(qr_copy, (qr_x, side_padding), qr_copy)
                    except Exception as e:
                        print(f"Error processing QR code: {e}")
                
                # Draw footer text
                branding_lines = []
                if practice_name:
                    branding_lines.append(practice_name)
                if plus_code:
                    branding_lines.append(plus_code)
                
                if branding_lines:
                    font_size_base = max(int(footer_height * 0.18), 24)
                    footer_rect = (0, footer_y_start, img.width, img.height + footer_height)
                    draw_footer_text(draw, branding_lines, footer_rect, font_size_base)
                
                # Get filename
                if i < len(file_names) and file_names[i]:
                    base_name = file_names[i]
                    base_name = os.path.splitext(base_name)[0]
                    base_name = secure_filename(base_name)
                else:
                    base_name = f"photo_{i+1}"
                
                output_name = f"{base_name}.jpg"
                
                # Save to BytesIO
                output_buffer = BytesIO()
                rgb_img = clean_img.convert('RGB')
                rgb_img.save(output_buffer, 'JPEG', quality=95)
                output_buffer.seek(0)
                
                # Upload to Blob
                processed_url = upload_to_blob(output_buffer.read(), output_name, 'image/jpeg')
                processed_urls.append({
                    'url': processed_url,
                    'filename': output_name
                })
                
            except Exception as e:
                print(f"Error processing photo {i}: {e}")
                continue
        
        if not processed_urls:
            return jsonify({'error': 'Failed to process any photos'}), 500
        
        return jsonify({
            'success': True,
            'files': processed_urls
        })
        
    except Exception as e:
        print(f"Error in process_photos: {e}")
        return jsonify({'error': f'An error occurred: {str(e)}'}), 500


# Keep legacy upload endpoint for backwards compatibility
@app.route('/upload', methods=['POST'])
def upload():
    return jsonify({'error': 'Direct uploads are not supported. Please use Blob storage.'}), 400


@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(
        app.config['PROCESSED_FOLDER'],
        filename,
        as_attachment=True
    )

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
