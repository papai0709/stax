#!/bin/zsh

# STAX Enhanced Service Manager
# Supports both Docker and local Python execution

SCRIPT_DIR=$(dirname "$0")
PROJECT_ROOT=$(dirname "$SCRIPT_DIR")
cd "$PROJECT_ROOT"

# Color codes for better output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${BLUE}[STAX]${NC} $1"
}

# Function to check if Docker is available and running
check_docker() {
    if ! command -v docker &> /dev/null; then
        return 1
    fi
    
    if ! docker info &> /dev/null; then
        return 1
    fi
    
    return 0
}

# Function to check if configuration exists
check_config() {
    if [ ! -f "config/.env" ]; then
        print_warning "No .env file found in config/"
        if [ -f "config/.env.example" ]; then
            print_status "Creating .env from template..."
            cp config/.env.example config/.env
            print_warning "Please edit config/.env with your actual values"
        else
            print_error "No .env.example found. Please create config/.env manually"
            return 1
        fi
    fi
    
    if [ ! -f "config/monitor_config.json" ]; then
        print_error "config/monitor_config.json not found"
        return 1
    fi
    
    return 0
}

# Function to start with Docker
start_docker() {
    print_header "Starting STAX with Docker..."
    
    if ! check_docker; then
        print_error "Docker is not available or not running"
        return 1
    fi
    
    print_status "Using Docker Compose in deploy/ folder"
    cd deploy
    
    # Check if containers are already running
    if docker-compose ps | grep -q "stax-app.*Up"; then
        print_warning "STAX container already running"
        docker-compose logs --tail=10
        return 0
    fi
    
    # Start with Docker Compose
    print_status "Building and starting containers..."
    docker-compose up --build -d
    
    if [ $? -eq 0 ]; then
        print_status "STAX started successfully with Docker!"
        print_status "🌐 Application: http://localhost:5001"
        print_status "🔍 Health Check: http://localhost:5001/api/health"
        print_status "📊 View logs: docker-compose logs -f"
    else
        print_error "Failed to start STAX with Docker"
        return 1
    fi
}

# Function to start locally
start_local() {
    print_header "Starting STAX locally..."
    
    # Check Python
    if ! command -v python3 &> /dev/null; then
        print_error "Python 3 is not available"
        return 1
    fi
    
    # Load environment
    if [ -f "config/.env" ]; then
        print_status "Loading environment from config/.env"
        export $(grep -v '^#' config/.env | xargs)
    fi
    
    # Kill existing processes
    print_status "Stopping any existing services..."
    pkill -f "python.*monitor_daemon.py" || true
    pkill -f "python.*main.py" || true
    
    # Start the main application
    print_status "Starting STAX application..."
    python3 scripts/monitor_daemon.py --mode api --port 5001 --config config/monitor_config.json &
    MAIN_PID=$!
    
    # Wait a moment and check if it's running
    sleep 3
    if ! kill -0 $MAIN_PID 2>/dev/null; then
        print_error "Failed to start STAX application"
        # Print logs to help debugging
        echo "Last 10 lines of log:"
        tail -n 10 logs/epic_monitor.log 2>/dev/null || tail -n 10 logs/enhanced_epic_monitor.log
        return 1
    fi
    
    print_status "STAX started successfully locally!"
    print_status "🌐 Application: http://localhost:5001"
    print_status "📝 PID: $MAIN_PID"
    
    # Wait for the process (but don't block in background usage, stax-manager typically runs and exits or blocks?)
    # The original script blocked with 'wait $MAIN_PID', so we should probably keep that behavior if 'start' implies running.
    # But usually 'start' means start in background. The original script had 'start_docker' as -d (background) 
    # but 'start_local' had 'wait $MAIN_PID' which blocks.
    # Users usually expect ./script start to return.
    
    # If we want to keep running in foreground, we wait. If we want background, we don't.
    # Given start_docker uses -d, start_local probably SHOULD be background, but the previous code was blocking.
    # I will keep the existing pattern of blocking for now to be safe, or I can make it background.
    # The user complained about inconsistent execution mode in the summary!
    # "start_local keeps the terminal blocked ... start_docker runs in background"
    # User proposed: "Fix Local Execution: Modify start_local to run ... in background".
    
    # So I should make it background!
    # Disown the process so it keeps running
    disown $MAIN_PID
}

# Function to stop services
stop_services() {
    print_header "Stopping STAX services..."
    
    # Stop Docker containers
    if check_docker && [ -f "deploy/docker-compose.yml" ]; then
        cd deploy
        if docker-compose ps | grep -q "stax-app"; then
            print_status "Stopping Docker containers..."
            docker-compose down
        fi
        cd ..
    fi
    
    # Stop local processes
    print_status "Stopping local processes..."
    pkill -f "python.*monitor_daemon.py" || true
    pkill -f "python.*main.py" || true
    
    print_status "All STAX services stopped"
}

# Function to show status
show_status() {
    print_header "STAX Service Status"
    
    # Check Docker
    if check_docker && [ -f "deploy/docker-compose.yml" ]; then
        cd deploy
        if docker-compose ps | grep -q "stax-app.*Up"; then
            print_status "✅ Docker container is running"
            docker-compose ps
        else
            print_status "❌ Docker container is not running"
        fi
        cd ..
    fi
    
    # Check local processes
    if pgrep -f "python.*main.py" > /dev/null; then
        print_status "✅ Local Python process is running"
        ps aux | grep -E "python.*main.py" | grep -v grep
    else
        print_status "❌ Local Python process is not running"
    fi
    
    # Check port
    if nc -z localhost 5001 2>/dev/null; then
        print_status "✅ Service responding on port 5001"
    else
        print_status "❌ No service responding on port 5001"
    fi
}

# Main script logic
case "${1:-start}" in
    "start")
        if ! check_config; then
            exit 1
        fi
        
        # Try Docker first, fall back to local
        if check_docker; then
            start_docker
        else
            print_warning "Docker not available, starting locally"
            start_local
        fi
        ;;
    "docker")
        if ! check_config; then
            exit 1
        fi
        start_docker
        ;;
    "local")
        if ! check_config; then
            exit 1
        fi
        start_local
        ;;
    "stop")
        stop_services
        ;;
    "status")
        show_status
        ;;
    "restart")
        stop_services
        sleep 2
        if ! check_config; then
            exit 1
        fi
        if check_docker; then
            start_docker
        else
            start_local
        fi
        ;;
    *)
        echo "Usage: $0 {start|docker|local|stop|status|restart}"
        echo ""
        echo "Commands:"
        echo "  start   - Start STAX (Docker preferred, fallback to local)"
        echo "  docker  - Force start with Docker"
        echo "  local   - Force start locally"
        echo "  stop    - Stop all STAX services"
        echo "  status  - Show service status"
        echo "  restart - Stop and start services"
        exit 1
        ;;
esac