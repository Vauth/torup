apt update -y
apt install ffmpeg
pip3 install -r requirements.txt --break-system-packages
python3 main.py
