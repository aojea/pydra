.PHONY: all protos clean

all: protos

protos:
	mkdir -p proto
	curl -sSL -o proto/pluginregistration.proto https://raw.githubusercontent.com/kubernetes/kubernetes/master/staging/src/k8s.io/kubelet/pkg/apis/pluginregistration/v1/api.proto
	curl -sSL -o proto/dra.proto https://raw.githubusercontent.com/kubernetes/kubernetes/master/staging/src/k8s.io/kubelet/pkg/apis/dra/v1/api.proto
	mkdir -p pydra/core/generated/pluginregistration
	mkdir -p pydra/core/generated/dra
	touch pydra/core/generated/__init__.py
	touch pydra/core/generated/pluginregistration/__init__.py
	touch pydra/core/generated/dra/__init__.py
	.venv/bin/python3 -m grpc_tools.protoc -Iproto --python_out=pydra/core/generated/pluginregistration --grpc_python_out=pydra/core/generated/pluginregistration pluginregistration.proto
	.venv/bin/python3 -m grpc_tools.protoc -Iproto --python_out=pydra/core/generated/dra --grpc_python_out=pydra/core/generated/dra dra.proto
	sed -i 's/import dra_pb2/from . import dra_pb2/g' pydra/core/generated/dra/dra_pb2_grpc.py
	sed -i 's/import pluginregistration_pb2/from . import pluginregistration_pb2/g' pydra/core/generated/pluginregistration/pluginregistration_pb2_grpc.py

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
