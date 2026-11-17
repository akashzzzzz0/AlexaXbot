FROM python:3.12-alpine
WORKDIR /app
COPY app.py .
COPY conversation_brain.py .
COPY model_runtime.py .
COPY ai_router.py .
COPY bootstrap.py .

# Validate all runtime modules during image build, not after deployment.
RUN python -m py_compile app.py conversation_brain.py model_runtime.py ai_router.py bootstrap.py \
    && python -c "import conversation_brain, model_runtime, ai_router; assert callable(ai_router.route_response)"

ENV PORT=3000
EXPOSE 3000
CMD ["python", "bootstrap.py"]
