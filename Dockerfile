FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home harbour && mkdir /data && chown harbour:harbour /data
COPY harbour ./harbour
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "harbour.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
