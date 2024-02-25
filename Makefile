
build:		
	rsync -av * server:~/code/garmin-stats
	ssh server "cd ~/code/garmin-stats; docker build --tag gcr.io/dworken-k8s/garmin ."

deploy: build
	ssh server "docker push gcr.io/dworken-k8s/garmin"
	ssh monoserver "cd ~/infra/ && docker compose pull garmin && docker compose rm -svf garmin && docker compose up -d garmin"
