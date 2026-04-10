# Use a base image with Python and Playwright dependencies
# Based on Playwright's official Docker images for consistent environment
FROM mcr.microsoft.com/playwright:v1.43.0-jammy

# Set working directory
WORKDIR /app

# Copy application files
COPY ./external_crawler /app/external_crawler

# Install Python dependencies
# Ensure all required Python packages are listed in requirements.txt
# For now, let's assume `fastapi`, `uvicorn`, `requests`, `pydantic`, `beautifulsoup4`
COPY ./external_crawler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port the FastAPI app will run on
EXPOSE 8001

# Command to run the FastAPI application
CMD ["uvicorn", "external_crawler.main:app", "--host", "0.0.0.0", "--port", "8001"]

