# Use an official lightweight Python image.
FROM python:3.9-slim

# Allow statements and log messages to immediately appear in the Knative logs
ENV PYTHONUNBUFFERED True

# Copy local code to the container image.
ENV APP_HOME /app
WORKDIR $APP_HOME
COPY . ./

# Install production dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# Run the web service on container startup. 
# Workers: 1, Threads: 8 is good for I/O bound tasks (calling APIs/BQ)
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 --keep-alive 65 main:app
