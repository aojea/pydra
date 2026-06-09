.PHONY: all protos clean build-images

all: protos

REGISTRY ?= ghcr.io/aojea/pydra
TAG ?= $(shell git describe --tags --always --dirty)

DRIVERS = amd network nvidia tpu

build-images:
	@for driver in $(DRIVERS); do \
		IMAGE="$(REGISTRY)/$$driver:$(TAG)"; \
		echo "Building $$IMAGE..."; \
		docker build --load -t $$IMAGE -f kubernetes/$$driver/Dockerfile . ; \
	done

PYTHON ?= .venv/bin/python3

protos:
	mkdir -p proto
	curl -sSL -o proto/pluginregistration.proto https://raw.githubusercontent.com/kubernetes/kubernetes/master/staging/src/k8s.io/kubelet/pkg/apis/pluginregistration/v1/api.proto
	curl -sSL -o proto/dra.proto https://raw.githubusercontent.com/kubernetes/kubernetes/master/staging/src/k8s.io/kubelet/pkg/apis/dra/v1/api.proto
	curl -sSL -o proto/deviceplugin.proto https://raw.githubusercontent.com/kubernetes/kubernetes/master/staging/src/k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1/api.proto
	mkdir -p pydra/core/generated/pluginregistration
	mkdir -p pydra/core/generated/dra
	mkdir -p pydra/core/generated/deviceplugin
	touch pydra/core/generated/__init__.py
	touch pydra/core/generated/pluginregistration/__init__.py
	touch pydra/core/generated/dra/__init__.py
	touch pydra/core/generated/deviceplugin/__init__.py
	$(PYTHON) -m grpc_tools.protoc -Iproto --python_out=pydra/core/generated/pluginregistration --grpc_python_out=pydra/core/generated/pluginregistration pluginregistration.proto
	$(PYTHON) -m grpc_tools.protoc -Iproto --python_out=pydra/core/generated/dra --grpc_python_out=pydra/core/generated/dra dra.proto
	$(PYTHON) -m grpc_tools.protoc -Iproto --python_out=pydra/core/generated/deviceplugin --grpc_python_out=pydra/core/generated/deviceplugin deviceplugin.proto
	sed -i 's/import dra_pb2/from . import dra_pb2/g' pydra/core/generated/dra/dra_pb2_grpc.py
	sed -i 's/import pluginregistration_pb2/from . import pluginregistration_pb2/g' pydra/core/generated/pluginregistration/pluginregistration_pb2_grpc.py
	sed -i 's/import deviceplugin_pb2/from . import deviceplugin_pb2/g' pydra/core/generated/deviceplugin/deviceplugin_pb2_grpc.py

clean:
	rm -rf proto pydra/core/generated

test-integration:
	./tests/run_integration.sh

verify:
	./hack/verify-protos.sh
	./hack/verify-lint.sh

verify-protos:
	./hack/verify-protos.sh

verify-lint:
	./hack/verify-lint.sh
