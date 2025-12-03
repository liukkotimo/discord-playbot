.PHONY: build push up down

IMAGE_NAME=discord-playbot
REMOTE_REPO=timoliukko42

build:
	docker build -f Dockerfile-playbot -t $(IMAGE_NAME):latest .
ifdef version
	docker tag $(IMAGE_NAME):latest $(IMAGE_NAME):$(version)
else
	echo "No version given"
endif

push: build
	docker tag $(IMAGE_NAME):latest $(REMOTE_REPO)/$(IMAGE_NAME):latest
	docker push $(REMOTE_REPO)/$(IMAGE_NAME):latest

up:
	docker-compose -f playbot-devel.yaml up --build

down:
	docker-compose -f playbot-devel.yaml down

prune:
	docker image prune
	docker system prune
	docker volume prune



