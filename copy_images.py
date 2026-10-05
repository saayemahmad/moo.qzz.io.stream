import os
import shutil
import re

SOURCE_FOLDER = "media"
DESTINATION_FOLDER = "copied_images"

# নতুন ফোল্ডার তৈরি করা (যদি আগে থেকে না থাকে)
if not os.path.exists(DESTINATION_FOLDER):
    os.makedirs(DESTINATION_FOLDER)
    print(f"Created folder: '{DESTINATION_FOLDER}'")

# ফাইলের নাম থেকে IMG এর পরের নাম্বার খোঁজার জন্য রেগুলার এক্সপ্রেশন
# এটি IMG381, img_381, IMG-381 সব ধরনের প্যাটার্ন ধরতে পারবে
pattern = re.compile(r'img.*?(\d+)', re.IGNORECASE)

copied_count = 0

print(f"Scanning '{SOURCE_FOLDER}'...")

if os.path.exists(SOURCE_FOLDER):
    for root, dirs, files in os.walk(SOURCE_FOLDER):
        for filename in files:
            # শুধু ছবি ফাইলগুলো চেক করব
            ext = filename.lower().split('.')[-1]
            if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                
                # ফাইলের নামে রেগুলার এক্সপ্রেশন ম্যাচ করানো
                match = pattern.search(filename)
                if match:
                    # নাম্বারটি বের করে ইন্টিজারে রূপান্তর করা
                    number = int(match.group(1))
                    
                    # যদি নাম্বারটি 381 থেকে 441 এর মাঝে হয়
                    if 381 <= number <= 441:
                        source_path = os.path.join(root, filename)
                        
                        # একই নামের ফাইল ডুপ্লিকেট এড়াতে ফোল্ডারের নাম যুক্ত করে নতুন নাম দেওয়া যায়
                        # তবে আপাতত শুধু ফাইলের নামেই কপি করছি
                        dest_path = os.path.join(DESTINATION_FOLDER, filename)
                        
                        # যদি একই নামের ফাইল আগে থেকেই থাকে, তবে তার সাথে একটি ইনডেক্স যোগ করে দিব
                        counter = 1
                        while os.path.exists(dest_path):
                            name, ex = os.path.splitext(filename)
                            dest_path = os.path.join(DESTINATION_FOLDER, f"{name}_{counter}{ex}")
                            counter += 1
                            
                        # কপি করা
                        shutil.copy2(source_path, dest_path)
                        print(f"Copied: {filename} -> {dest_path}")
                        copied_count += 1

    print(f"\nTotal {copied_count} images copied!")
else:
    print(f"Folder '{SOURCE_FOLDER}' not found!")
