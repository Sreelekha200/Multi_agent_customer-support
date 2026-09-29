FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY evals ./evals
COPY scripts ./scripts

RUN pip install --no-cache-dir .

ENV APP_ENV=production
ENV DATABASE_URL=sqlite:///./data/support.db
RUN mkdir -p /app/data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
