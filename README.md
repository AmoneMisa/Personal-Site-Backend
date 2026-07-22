# Personal Site Backend

Backend API powering [whiteslove.me](https://whiteslove.me) — handles data for the site's developer tools and utilities (PDF editor, JSON merger, DockerHub search, and more).

## Tech Stack
- **Language:** Python
- **Web server:** Nginx (reverse proxy)
- **Containerization:** Docker & Docker Compose
- **CI/CD:** GitHub Actions

## Features
- REST API serving the frontend (whiteslove.me)
- Containerized deployment with Docker Compose
- Nginx configured as a reverse proxy in front of the API
- Automated build/deploy pipeline via GitHub Actions

## Project Structure
├── src/ # Application source code
├── .github/workflows/ # CI/CD pipeline definitions
├── Dockerfile
├── docker-compose.yml
├── nginx.conf
└── requirements.txt

## Getting Started

### Prerequisites
- Docker & Docker Compose
- Python 3.x (for local development without Docker)

### Setup
1. Clone the repository:
```bash
   git clone https://github.com/AmoneMisa/Personal-Site-Backend.git
   cd Personal-Site-Backend
```
2. Copy the example environment file and fill in your values:
```bash
   cp sample.env .env
```
3. Build and run with Docker Compose:
```bash
   docker-compose up --build
```

## Related
- Frontend: [whiteslove.me](https://whiteslove.me)

## License
Personal project, shared for demonstration purposes.
