import os
import socket
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer

# আপনার ফোল্ডারের নাম (যেখানে ছবিগুলো আছে)
MEDIA_FOLDER = "media"

class ImageGalleryHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # যদি ইউজার হোমপেজে (/) আসে, তবে আমরা HTML গ্যালারি দেখাবো
        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            
            # HTML টেমপ্লেট
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Media Gallery</title>
                <style>
                    body {{ font-family: sans-serif; background: #111; color: #fff; padding: 20px; }}
                    h1 {{ text-align: center; color: #00d2ff; }}
                    .gallery {{ display: flex; flex-wrap: wrap; gap: 20px; justify-content: center; }}
                    .card {{ background: #222; padding: 10px; border-radius: 8px; text-align: center; max-width: 200px; }}
                    img {{ max-width: 100%; height: auto; border-radius: 4px; }}
                    .name {{ margin-top: 10px; font-size: 14px; word-break: break-all; color: #aaa; }}
                </style>
            </head>
            <body>
                <h1>Media Thumbnails</h1>
                <div class="gallery">
            """
            
            # Media ফোল্ডার এবং এর ভেতরের সব ফোল্ডার স্ক্যান করে ছবিগুলো বের করা
            if os.path.exists(MEDIA_FOLDER):
                for root, dirs, files in os.walk(MEDIA_FOLDER):
                    for filename in files:
                        ext = filename.lower().split('.')[-1]
                        # শুধু সেই ছবিগুলো নেবো যেগুলোর নামে 'thumb' শব্দটি আছে
                        if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp'] and 'thumb' in filename.lower():
                            # subfolder সহ relative path তৈরি করা
                            rel_path = os.path.relpath(os.path.join(root, filename), start='.')
                            rel_path = rel_path.replace('\\', '/') # Windows path fix for URLs
                            
                            # ছবির কার্ড তৈরি করা
                            html += f"""
                            <div class="card">
                                <img src="/{rel_path}" alt="{filename}">
                                <div class="name">{filename}<br><small style="color:#666; font-size:10px;">{rel_path}</small></div>
                            </div>
                            """
            else:
                html += f"<p>'{MEDIA_FOLDER}' ফোল্ডারটি পাওয়া যায়নি!</p>"
                
            html += """
                </div>
            </body>
            </html>
            """
            self.wfile.write(html.encode('utf-8'))
        else:
            # অন্যান্য ফাইলের জন্য (যেমন ছবির আসল ফাইল) ডিফল্ট হ্যান্ডলার কাজ করবে
            super().do_GET()

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

if __name__ == "__main__":
    port = find_free_port()
    # current directory থেকে সার্ভার রান করবে
    with TCPServer(("", port), ImageGalleryHandler) as httpd:
        print(f"Server is running at: http://localhost:{port}")
        print("Press CTRL+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
