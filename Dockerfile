FROM python:3.12-bookworm
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD FLASK_APP=zones.py flask run --host=0.0.0.0 --port 8000