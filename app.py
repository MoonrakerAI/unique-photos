# This is a simple web application using Flask to allow users to upload logos and QR codes to client photos.

from flask import Flask, request, send_file, render_template, jsonify, send_from_directory, make_response
from PIL import Image, ImageDraw, ImageFont, ImageOps
import os
import zipfile
import io
import shutil
import qrcode
from io import BytesIO
from werkzeug.utils import secure_filename

app = Flask(__name__)

# CORS support for Vercel
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# Use /tmp for Vercel serverless environment
IS_VERCEL = os.environ.get('VERCEL', False)
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

    # Use a slightly smaller font for secondary lines
    font_main = load_font(int(font_size_base))
    font_sub = load_font(int(font_size_base * 0.8))
    
    line_spacing = int(font_size_base * 0.2)
    
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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    try:
        # Handle logo upload
        logo = None
        if 'logo' in request.files and request.files['logo'].filename != '':
            logo_file = request.files['logo']
            if not allowed_file(logo_file.filename):
                return jsonify({'error': 'Invalid logo file type. Allowed types: png, jpg, jpeg, gif'}), 400
            
            # Save logo to a temporary file
            logo_path = os.path.join(app.config['UPLOAD_FOLDER'], 'temp_logo.png')
            logo_file.save(logo_path)
            logo = logo_path
        
        # Handle QR code (either uploaded image, URL, or generated from Google Place ID)
        qr_code = None
        qr_url = request.form.get('qr_url', '').strip()
        practice_name = request.form.get('practice_name', '').strip()
        plus_code = request.form.get('plus_code', '').strip()
        qr_url_generated = False
        
        if qr_url:
            # Clean Google Maps URL if needed (remove tracking parameters)
            is_google_maps = any(domain in qr_url for domain in ['google.com/maps', 'maps.google.com', 'maps.app.goo.gl'])
            if is_google_maps and '?' in qr_url:
                qr_url = qr_url.split('?')[0]
                
            # Generate QR code from URL
            try:
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_H,
                    box_size=10,
                    border=4,
                )
                qr.add_data(qr_url)
                qr.make(fit=True)
                
                # Create QR code image
                qr_img = qr.make_image(fill_color="black", back_color="white")
                
                # Save to a temporary file
                qr_path = os.path.join(app.config['UPLOAD_FOLDER'], 'generated_qr.png')
                qr_img.save(qr_path)
                qr_code = qr_path
                
            except Exception as e:
                if os.path.exists(logo):
                    os.remove(logo)
                return jsonify({'error': f'Error generating QR code: {str(e)}'}), 400
    
        # Check if photos were uploaded
        if 'photos' not in request.files or not any(f for f in request.files.getlist('photos') if f.filename):
            if logo and os.path.exists(logo):
                os.remove(logo)
            if qr_code and os.path.exists(qr_code):
                os.remove(qr_code)
            return jsonify({'error': 'No photos uploaded'}), 400
            
        photos = request.files.getlist('photos')
        # Filter out empty file inputs and validate file types
        valid_photos = []
        for photo in photos:
            if photo and photo.filename and allowed_file(photo.filename):
                valid_photos.append(photo)
        
        if not valid_photos:
            if logo and os.path.exists(logo):
                os.remove(logo)
            if qr_code and os.path.exists(qr_code):
                os.remove(qr_code)
            return jsonify({'error': 'No valid photos uploaded. Supported formats: png, jpg, jpeg, gif'}), 400
        
        # Get file names from form data
        file_names = request.form.getlist('file_names')
        
        # If no names provided or not enough, use original filenames
        if not file_names or len(file_names) != len(valid_photos):
            file_names = [os.path.splitext(photo.filename)[0] for photo in valid_photos]
        
        processed_files = []
        output_filenames = []
        
        # Process each photo
        for i, photo in enumerate(valid_photos):
            try:
                # Create a safe filename
                filename = secure_filename(photo.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                photo.save(file_path)
                
                # Open the image and process it
                with Image.open(file_path) as img:
                    # Fix orientation based on EXIT data
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
                    # Reduce padding for larger elements and closer-to-edge look
                    side_padding = int(img.width * 0.02)
                    top_bottom_padding = int(footer_height * 0.1)
                    content_height = footer_height - (top_bottom_padding * 2)
                    
                    # Define consistent fixed height for top elements (360px)
                    target_element_height = 360
                    
                    # Process logo if provided (Top Left)
                    if logo and os.path.exists(logo):
                        try:
                            with Image.open(logo) as logo_img:
                                logo_img = logo_img.convert('RGBA')
                                # Logo size: Keep height locked to target, allow width to be up to 30%
                                logo_img.thumbnail((int(img.width * 0.3), target_element_height), Image.LANCZOS)
                                
                                # Apply 50% opacity to logo
                                alpha = logo_img.split()[3]
                                alpha = alpha.point(lambda p: int(p * 0.5))
                                logo_img.putalpha(alpha)
                                
                                # Position at Top Left with padding
                                clean_img.paste(logo_img, (side_padding, side_padding), logo_img)
                        except Exception as e:
                            print(f"Error processing logo: {e}")
                    
                    # Process QR code if provided (Top Right - Sync height with logo)
                    if qr_code and os.path.exists(qr_code):
                        try:
                            with Image.open(qr_code) as qr_img:
                                qr_img = qr_img.convert('RGBA')
                                # QR size: Lock height to target_element_height
                                qr_img.thumbnail((target_element_height, target_element_height), Image.LANCZOS)
                                
                                # Apply 50% opacity to QR code
                                alpha = qr_img.split()[3]
                                alpha = alpha.point(lambda p: int(p * 0.5))
                                qr_img.putalpha(alpha)
                                
                                # Position at Top Right with padding
                                qr_x = img.width - qr_img.width - side_padding
                                clean_img.paste(qr_img, (qr_x, side_padding), qr_img)
                        except Exception as e:
                            print(f"Error processing QR code: {e}")
                    
                    branding_lines = []
                    if practice_name:
                        branding_lines.append(practice_name)
                    if plus_code:
                        branding_lines.append(plus_code)
                    
                    if branding_lines:
                        # Draw centered text in the middle of the footer
                        font_size_base = max(int(footer_height * 0.2), 16)
                        footer_rect = (0, footer_y_start, img.width, img.height + footer_height)
                        draw_footer_text(draw, branding_lines, footer_rect, font_size_base)
                    
                    # Generate output filename with original extension
                    original_ext = os.path.splitext(photo.filename)[1].lower()
                    if not original_ext or original_ext not in ['.png', '.jpg', '.jpeg', '.gif']:
                        original_ext = '.png'
                    
                    # Get base name from custom pattern or original filename
                    # Strip any existing extension from the base name to prevent double extensions
                    base_name = file_names[i] if i < len(file_names) else f"photo_{i+1}"
                    base_name = os.path.splitext(base_name)[0]  # Remove any extension
                    base_name = secure_filename(base_name)
                    
                    output_name = f"{base_name}{original_ext}"
                    output_path = os.path.join(PROCESSED_FOLDER, output_name)
                    
                    # Ensure unique filename
                    counter = 1
                    while os.path.exists(output_path):
                        output_name = f"{base_name}_{counter}{original_ext}"
                        output_path = os.path.join(PROCESSED_FOLDER, output_name)
                        counter += 1
                    
                    # Ensure output directory exists
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                    
                    # Save the processed image with the correct format
                    if output_name.lower().endswith('.png'):
                        clean_img.save(output_path, 'PNG')
                    else:
                        rgb_img = clean_img.convert('RGB')
                        rgb_img.save(output_path, 'JPEG', quality=95)
                    
                    processed_files.append(output_path)
                    output_filenames.append(os.path.basename(output_path))
                    
                    # Clean up the uploaded file
                    os.remove(file_path)
            
            except Exception as e:
                app.logger.error(f"Error processing {photo.filename}: {str(e)}")
                continue
            
        # Clean up temporary files
        if logo and os.path.exists(logo):
            os.remove(logo)
        if qr_code and os.path.exists(qr_code):
            os.remove(qr_code)
        
        if not processed_files:
            return jsonify({'error': 'Failed to process any photos'}), 500
        
        # Return the list of processed files
        return jsonify({
            'success': True,
            'files': output_filenames
        })
        
    except Exception as e:
        # Clean up any remaining files in case of error
        if 'logo' in locals() and logo and os.path.exists(logo):
            os.remove(logo)
        if 'qr_code' in locals() and qr_code and os.path.exists(qr_code):
            os.remove(qr_code)
        return jsonify({'error': f'An error occurred during processing: {str(e)}'}), 500

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(
        app.config['PROCESSED_FOLDER'],
        filename,
        as_attachment=True
    )

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
