from setuptools import setup, find_packages

setup(
    name="ComputerRecorder",
    version="0.1.0",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        # Core dependencies (cross-platform)
        "pillow",  # For image processing
        # --- macOS screen/input stack (unused on Linux/Wayland) ---
        "mss; sys_platform == 'darwin'",  # Screen capture (X11-only elsewhere)
        "pynput; sys_platform == 'darwin'",  # Mouse/keyboard (X11-only elsewhere)
        "shapely; sys_platform == 'darwin'",  # Geometry for window occlusion
        "pyobjc-framework-Quartz; sys_platform == 'darwin'",  # macOS windows
        "openai>=1.0.0",
        "SQLAlchemy>=2.0.0",
        "pydantic>=2.0.0",
        "sqlalchemy-utils>=0.41.0",
        "python-dotenv>=1.0.0",
        "scikit-learn",
        "aiosqlite",
        "greenlet",
        "PyYAML",  # For Google Drive settings configuration
        "PyDrive",  # For Google Drive integration
        # Google Drive API dependencies (optional, for advanced features)
        "google-auth",  # For Google Drive API authentication
        "google-auth-oauthlib",  # For OAuth flow
        "google-auth-httplib2",  # For HTTP requests
        "google-api-python-client",  # For Google Drive API
    ],
    extras_require={
        'monitoring': [
            'psutil',  # For memory monitoring
        ],
        # Linux/Wayland screen + input backends.  These wrap system libraries
        # (PipeWire, GLib, libxkbcommon), so installing the matching distro
        # packages and creating the venv with --system-site-packages is more
        # reliable than building them from source.  On Fedora:
        #   sudo dnf install python3-gobject python3-evdev python3-xkbcommon \
        #       gstreamer1-plugin-pipewire
        #   sudo usermod -aG input $USER   # then log out and back in
        'linux': [
            "PyGObject; sys_platform == 'linux'",   # GLib/Gio/Gst bindings
            "evdev; sys_platform == 'linux'",       # Raw input devices
            "xkbcommon; sys_platform == 'linux'",   # Keycode -> character
        ],
    },
    entry_points={
        'console_scripts': [
            'crec=crec.cli:main',
        ],
    },
    description="A Python package with command-line interface",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.7",
) 