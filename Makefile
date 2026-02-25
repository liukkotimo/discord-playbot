.PHONY: build push up down prune

IMAGE_NAME=discord-playbot
REMOTE_REPO=timoliukko42
PLATFORMS=linux/amd64,linux/arm64

setup-buildx:
	docker buildx create --use --name mybuilder || docker buildx use mybuilder

build: setup-buildx
	docker buildx build --platform $(PLATFORMS) -f Dockerfile-playbot -t $(IMAGE_NAME):latest . --load
ifdef version
	docker tag $(IMAGE_NAME):latest $(IMAGE_NAME):$(version)
else
	@echo "No version given"
endif

push: setup-buildx
	docker buildx build --platform $(PLATFORMS) -f Dockerfile-playbot \
		-t $(REMOTE_REPO)/$(IMAGE_NAME):latest \
		$(if $(version),-t $(REMOTE_REPO)/$(IMAGE_NAME):$(version)) \
		--push .

up:
	docker-compose -f playbot-devel.yaml up --build

down:
	docker-compose -f playbot-devel.yaml down

prune:
	docker image prune -f
	docker system prune -f
	docker volume prune -f