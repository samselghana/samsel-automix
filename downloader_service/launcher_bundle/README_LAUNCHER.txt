One-click launcher for your downloader service

How to use
1. Copy these files into your downloader_service folder.
2. Double-click Start_Downloader_Service.bat to start the API.
3. Double-click Stop_Downloader_Service.bat to stop it.
4. If you want a quieter launch, double-click Downloader_Service_Launcher.vbs.

Expected folder
- downloader_service
  - .venv
  - app
  - requirements.txt
  - Start_Downloader_Service.bat
  - Stop_Downloader_Service.bat
  - Downloader_Service_Launcher.vbs

Notes
- The launcher expects the virtual environment to be inside .venv.
- The service runs on http://127.0.0.1:8010
- The launcher opens the health page automatically.
