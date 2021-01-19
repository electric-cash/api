.PHONY: build exec run

IMG?=elcash-api:local
ENVS?=-e RPC_HOST=127.0.0.1 \
	  -e RPC_USER=user \
	  -e RPC_PASSWORD=pass \
	  -e DB_URL=mongodbURL \
	  -e DB_NAME=mongoDBname \
	  -e DB_PORT=mongoDBport

build:
	docker build -t $(IMG) .

run:
	@docker run --rm -it \
    -v "$(PWD)/src:/app" $(IMG)

run-daemon:
	@docker run --rm -it $(ENVS) \
    -v "$(PWD)/src:/app" $(IMG) python3 daemon.py

run-api:
	@docker run --rm -it $(ENVS) \
	-p 5000:5000 \
    -v "$(PWD)/src:/app" $(IMG) gunicorn -w 3 --bind 0.0.0.0:5000 api:app


exec:
	@docker run --rm -it $(ENVS) \
	--entrypoint /bin/bash \
    -v "$(PWD)/src:/app" $(IMG)
