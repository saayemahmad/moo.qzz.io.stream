import tkinter as tk
import requests
import threading
import time

DB_URL = "https://dev-ca098-default-rtdb.firebaseio.com"

class Overlay(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        
        self.trans_color = '#000001'
        self.attributes('-transparentcolor', self.trans_color)
        self.config(bg=self.trans_color)
        
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")
        
        self.canvas = tk.Canvas(self, bg=self.trans_color, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        
        self.line = self.canvas.create_line(0, 0, 0, 0, fill='#32cd32', width=5, state='hidden')

    def update_line(self, x1, y1, x2, y2, show=True):
        if show:
            self.canvas.coords(self.line, x1, y1, x2, y2)
            self.canvas.itemconfig(self.line, state='normal')
        else:
            self.canvas.itemconfig(self.line, state='hidden')

class Widget(tk.Tk):
    def __init__(self):
        super().__init__()
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        self.attributes('-alpha', 0.95)
        
        self.default_bg = '#1e1e1e'
        self.blink_bg = '#5a1a1a' 
        
        self.configure(bg=self.default_bg)
        
        # Smaller geometry since we only have one label now
        self.geometry("140x50+100+100")
        
        self.bind("<ButtonPress-1>", self.start_move)
        self.bind("<ButtonRelease-1>", self.stop_move)
        self.bind("<B1-Motion>", self.do_move)
        
        self.bind("<Button-3>", self.show_context_menu)
        self.menu = tk.Menu(self, tearoff=0, bg='#2a2a2a', fg='white', borderwidth=0)
        self.menu.add_command(label="Close Widget", command=self.close_app)
        
        # Frame with a thick border for presence
        self.frame = tk.Frame(self, bg=self.default_bg, highlightbackground="#444", highlightthickness=4)
        self.frame.pack(fill='both', expand=True)

        self.sayem_lbl = tk.Label(self.frame, text="Unread: -", fg="#aaaaaa", bg=self.default_bg, font=("Segoe UI", 12, "bold"))
        self.sayem_lbl.pack(expand=True)
        
        for widget_elem in (self.frame, self.sayem_lbl):
            widget_elem.bind("<ButtonPress-1>", self.start_move)
            widget_elem.bind("<ButtonRelease-1>", self.stop_move)
            widget_elem.bind("<B1-Motion>", self.do_move)
            widget_elem.bind("<Button-3>", self.show_context_menu)
            
        self.unread_count = 0
        self.is_blink_on = False
        self.snooze_until = 0
        
        self.overlay = Overlay(self)
        
        self.update_loop()
        self.blink_loop()
        self.draw_loop()
        
    def start_move(self, event):
        self.x = event.x
        self.y = event.y
        if self.unread_count > 0:
            self.snooze_until = time.time() + 60

    def stop_move(self, event):
        self.x = None
        self.y = None

    def do_move(self, event):
        if self.x is not None and self.y is not None:
            deltax = event.x - self.x
            deltay = event.y - self.y
            x = self.winfo_x() + deltax
            y = self.winfo_y() + deltay
            self.geometry(f"+{x}+{y}")
            
    def close_app(self):
        self._is_running = False
        try:
            self.overlay.destroy()
        except:
            pass
        self.destroy()
        
    def show_context_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    def draw_loop(self):
        if not getattr(self, '_is_running', True):
            return
            
        try:
            if self.unread_count > 0 and time.time() > self.snooze_until:
                wx = self.winfo_x() + self.winfo_width() // 2
                wy = self.winfo_y() + self.winfo_height() // 2
                mx = self.winfo_pointerx()
                my = self.winfo_pointery()
                
                self.overlay.update_line(wx, wy, mx, my, show=True)
            else:
                self.overlay.update_line(0, 0, 0, 0, show=False)
        except tk.TclError:
            return
            
        self.after(16, self.draw_loop)

    def blink_loop(self):
        if not getattr(self, '_is_running', True):
            return
            
        try:
            if self.unread_count > 0 and time.time() > self.snooze_until:
                self.is_blink_on = not self.is_blink_on
                alpha = 0.0 if self.is_blink_on else 0.95
            else:
                self.is_blink_on = False
                alpha = 0.95
                
            self.attributes('-alpha', alpha)
        except tk.TclError:
            return
            
        self.after(600, self.blink_loop)

    def update_loop(self):
        threading.Thread(target=self.fetch_data, daemon=True).start()
        self.after(5000, self.update_loop)
        
    def fetch_data(self):
        try:
            p_res = requests.get(f"{DB_URL}/Presence/Shajeda.json")
            if p_res.status_code == 200:
                p_data = p_res.json()
                if p_data:
                    state = p_data.get('state', 'offline').lower()
                    if state == 'active':
                        color = '#32cd32' 
                    elif state == 'away':
                        color = '#ffa500' 
                    else:
                        color = '#ff4444' 
                    
                    self.frame.config(highlightbackground=color)
            
            ls_res = requests.get(f"{DB_URL}/Media_Chat_LastSeen/Sayem.json")
            sayem_ls = 0
            if ls_res.status_code == 200 and ls_res.text != 'null':
                sayem_ls = int(ls_res.text)
                
            params = {'orderBy': '"$key"', 'limitToLast': 100}
            m_res = requests.get(f"{DB_URL}/Media_Chat_Web.json", params=params)
            
            if m_res.status_code == 200:
                msgs = m_res.json()
                unread = 0
                if msgs:
                    for k, msg in msgs.items():
                        if isinstance(msg, dict) and msg.get('sender') != 'Sayem':
                            ts = msg.get('timestamp', 0)
                            if ts > sayem_ls:
                                unread += 1
                                
                self.unread_count = unread
                if unread > 0:
                    self.sayem_lbl.config(text=f"Unread: {unread}", fg="#ffffff")
                else:
                    self.snooze_until = 0
                    self.sayem_lbl.config(text=f"Unread: 0", fg="#aaaaaa")
                
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    app = Widget()
    app.mainloop()
