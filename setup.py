from setuptools import setup


setup(
    setup_requires=["setuptools_scm"],
    use_scm_version=True,
    name="metapkg",
    description="Cross-Platform Meta Packaging System",
    author="MagicStack Inc.",
    author_email="hello@magic.io",
    packages=["metapkg"],
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "metapkg = metapkg.app:main",
        ]
    },
    python_requires=">=3.9",
    install_requires=[
        "build~=1.2.1",
        "distro~=1.9.0",
        "requests~=2.31.0",
        "poetry~=2.1.2",
        "distlib~=0.3.8",
        'python-magic~=0.4.26; platform_system=="Linux" or (platform_machine!="x86_64" and platform_machine!="AMD64")',
        'python-magic-bin~=0.4.14; platform_system!="Linux" and platform_machine!="arm64"',
        "wheel>=0.32.3",
        "setuptools-rust>=0.11.4",
        "tomli>=1.2",
    ],
    extras_require={
        "test": [
            "typing-extensions~=4.0",
            "types-requests~=2.31.0.2",
        ]
    },
)
