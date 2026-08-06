@Library('netguard-lib') _

pipeline {
    agent any 

    environment {
        DEPLOY_DIR = "/opt/NetGuard/"
        IMAGE_TAG  = "${BUILD_NUMBER}"
        DOCKER_BUILDKIT = "1"
    }

    stages {
        stage('Checkout') {
            steps {
                    checkout scm
                }
            }

        stage('Dependencies') {
            parallel {
                stage('Backend Dependencies') 
                {
                steps {
                    backendDependencies()
                }
            }
                stage('Frontend Dependencies') 
                {
                steps {
                    frontendDependencies()
                }
            }
        }
    }

        stage('Lints') {
            parallel {
                stage('Backend Lint') {
                    steps {
                        backendLint()
                }
            }

                stage('Frontend Lint') {
                steps {
                    frontendLint()
                }
            }
        }
    }

        stage('Tests') {
            parallel {
                stage('backend tests') {
                steps {
                    backendTests()
                }
            }
        }
    }
        
        stage('Build  Images') {
            parallel {
                stage('Backend') {
                    steps {
                        dockerBuild(
                            image: "ghcr.io/nandhakumar-io/netguard-backend",
                            tag: IMAGE_TAG,
                            context: "./backend"
                        )
            }
        }

                stage('Frontend') {
                    steps {
                        dockerBuild(
                            image: "ghcr.io/nandhakumar-io/netguard-frontend",
                            tag: IMAGE_TAG,
                            context: "./frontend"
                        )
                }
            }
        }
    }

        stage('Push Images') {
                    steps {
                        script {

                            dockerLogin()

                            parallel (
                                backend: {
                                    dockerPush(
                                        image: "ghcr.io/nandhakumar-io/netguard-backend",
                                        tag: IMAGE_TAG
                                    )
                                },

                                frontend: {
                                    dockerPush(
                                        image: "ghcr.io/nandhakumar-io/netguard-frontend",
                                        tag: IMAGE_TAG
                                    )
                                }
                            )
                        }
                    }
                }

        stage('Deploy') {

            steps {

                script {

                    try {

                        deploy(IMAGE_TAG)

                        healthCheck()

                        updateVersion(IMAGE_TAG)
                    }

                    catch (Exception e) {

                        rollback()
                        
                        throw e 
                }
            }
        }
    }


    post {
        success {
                echo "Build Successful"
            }
        failure {
            echo "Build failure !..."
        }
        always {
            cleanWs()
            dockerLogout()
        }
    }
}
